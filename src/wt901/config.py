"""寄存器读写事务与输出配置。

读寄存器不是「发了就有」：它是请求/响应事务。发出 ``FF AA 27 <reg> 00`` 后设备
回一帧 ``0x55 0x71``，其中携带**起始地址起连续 8 个寄存器**的值。这些回帧与
``0x61`` 实时数据流混在同一条链路上到达，所以必须按地址把响应关联回请求，而不是
官方示例那样 sleep 一段时间再去翻缓存。

写寄存器是一个有时序的三步操作（解锁 → 写 → 保存），中间的延时不是可选的。本模块
把它封装成原子操作，调用方不感知时序，也无法把它拆开执行一半。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from wt901.errors import TransportTimeoutError, UnsupportedRegisterError
from wt901.protocol import commands
from wt901.protocol.frames import RegisterResponse
from wt901.protocol.registers import (
    AlgorithmMode,
    Bandwidth,
    Mounting,
    Register,
    ReturnRate,
    SaveAction,
)

if TYPE_CHECKING:
    from wt901.device import WT901Device

__all__ = [
    "DEFAULT_READ_TIMEOUT",
    "DEFAULT_SAVE_DELAY",
    "DEFAULT_WRITE_DELAY",
    "RegisterAccess",
    "Settings",
]

DEFAULT_READ_TIMEOUT = 0.5
"""秒。官方实现用 100 ms，自动读暂停时放宽到 700 ms。取中间值并配合重试。"""

DEFAULT_READ_RETRIES = 2
"""单次读失败后再试几次。总尝试次数是这个数 + 1。

**已实测，但实测没有给出改动它的依据。** 与上面的 `DEFAULT_READ_TIMEOUT` 不同
——那个数交代得出出处（官方实现的两个值取中间），这个数交代不出，至今仍然如此。

怀疑它在替一件没被记录下来的设备行为兜底：官方 SDK 的读数据线程连上后先
``Thread.sleep(5000)`` 才开始读任何寄存器，而且 ``0x72`` 在还没读到时**同一条读
指令紧挨着发两次**（其余寄存器都只发一次）。两处写法都指向「刚连上的一段时间里
寄存器读更容易失败」。本库连接后立即读寄存器（``read_device_info()`` 就是典型
用法），这两次重试可能一直在悄悄吸收那件事。

**2026-09-02 按 RAY-313 预注册的判据实测过**（`tools/probe_read_retries.py`，
2 台设备 × 各 3 次连接，三相 × 两寄存器 × 30 次，``read_retries = 0``）：

    1080 次读**零失败**——零超时、零回帧内容异常、零非超时错误。
    early 与 late 的失败率差 6/6 全是 0 个百分点（预注册门槛 15）；
    耗时中位比 6/6 全在 1.00–1.01（预注册门槛 1.5）。

按预注册判据这是「**测不出**」，**不是「不存在」**：N = 30 分辨不了更小的差别。
所以这个值**仍然没有依据**，只是现在知道得更具体了——不是「没测过」，而是
「测了，这个方法在这个量级上分辨不出来」。**改大改小依旧没有数据支撑。**

那次实测另外给了一条边界：early 相 360 次读零失败，失败率的 95% 置信上界约
**0.83%**。它约束了效应大小，但**约束不等于否证**——别据此把重试去掉。经过、
局限与那条耗时被 BLE 连接间隔量化的方法学事实，记在 ``docs/protocol.md`` §10，
证据在 ``ray-313/read-retry-verdict/acceptance/``。

代价不是零：每次真正失败的读要多花 ``read_timeout × read_retries``（默认 1.0 s），
而寄存器读与 ``0x61`` 实时流抢同一条链路。
"""

DEFAULT_WRITE_DELAY = 0.1
"""秒。解锁与写、写与保存之间的间隔，与官方实现一致。"""

DEFAULT_SAVE_DELAY = 0.5
"""``save`` 之后等待 flash 写入完成的时间，秒。

设备在执行 ``FF AA 00 00 00``（保存到 flash）期间会对部分寄存器回读出中间状态。
真机实测（RAY-182，两台设备）：``save`` 之后立刻读 ``0x64`` 电量寄存器，4/4 读到
0——而 0 是电压 ×100，物理上不可能。等 0.5 秒后 4/4 正常。逐轮测量恢复耗时为
299/301/300/302/327 ms，分布极窄，像是一个固定的 flash 写入时长。

**取 0.5 秒而不是 0.35 秒，是因为 0.5 秒是唯一被实测验证过的值**（对照实验里等
0.5 秒的那组 4/4 通过），0.35 秒只是从上界推断出来的。测量值本身含一次寄存器读的
往返，是恢复时间的**上界**而非其本身，贴着它取等于用推断替换实测。持久化写入不在
热路径上，这半秒是一次性代价。

只在 ``persist=True`` 时生效——不保存就不写 flash，也就没有这个窗口。重连后的配置
重放走 ``persist=False``，因此不受影响。
"""


@dataclass
class Settings:
    """一次配置事务里要改的项。``None`` 表示不动。"""

    output_rate: ReturnRate | int | None = None
    bandwidth: Bandwidth | int | None = None
    algorithm: AlgorithmMode | int | None = None
    mounting: Mounting | int | None = None


@dataclass(frozen=True, slots=True)
class _AppliedWrite:
    register: int
    value: int


def _coerce_return_rate(value: ReturnRate | int) -> ReturnRate:
    """把取值收敛到已核实的档位，其余一律拒绝。

    器件支持 0.2–200 Hz，但官方资料只演示了两个档位的编码。写入一个未经证实的
    编码可能让设备进入未知状态，而那种故障在现场表现为「数据不对但连接正常」，
    极难定位。宁可拒绝。
    """
    try:
        return ReturnRate(value)
    except ValueError:
        raise UnsupportedRegisterError(
            f"回传速率编码 0x{int(value):02X} 未在真机上核实。"
            f"当前已核实：{', '.join(f'{r.name}=0x{r.value:02X}' for r in ReturnRate)}。"
            "开放其余档位需先实测确认实际速率。"
        ) from None


def _coerce_algorithm_mode(value: AlgorithmMode | int) -> AlgorithmMode:
    """把取值收敛到手册登记的两档。

    只有 0 与 1 有文档定义。写入别的值不会报错，设备会静默进入未知状态——而
    「姿态数据不对但连接正常」是最难定位的一类故障。宁可拒绝。
    """
    try:
        return AlgorithmMode(value)
    except ValueError:
        raise UnsupportedRegisterError(
            f"算法模式 0x{int(value):02X} 不在手册登记的取值内。"
            f"当前已登记：{', '.join(f'{m.name}={m.value}' for m in AlgorithmMode)}。"
        ) from None


def _coerce_mounting(value: Mounting | int) -> Mounting:
    """把取值收敛到手册登记的两档。理由同 :func:`_coerce_algorithm_mode`。"""
    try:
        return Mounting(value)
    except ValueError:
        raise UnsupportedRegisterError(
            f"安装方向 0x{int(value):02X} 不在手册登记的取值内。"
            f"当前已登记：{', '.join(f'{m.name}={m.value}' for m in Mounting)}。"
        ) from None


def _coerce_bandwidth(value: Bandwidth | int) -> Bandwidth:
    """把取值收敛到协议文档列出的七档（``0x00``–``0x06``），其余一律拒绝。

    措辞与 :func:`_coerce_return_rate` 的「未核实」**有意不同**：速率的每一档都在
    真机上量过，带宽一档都没有（见 :class:`Bandwidth`）。这里拒绝的理由因此只有
    一条——防止**误**写一个没人看过的编码让设备进入未知状态，而那种故障在现场表现
    为「数据不对但连接正常」。

    七档齐备之后这条理由**没有变弱**，只是白名单的边界从「本库登记过的」挪到了
    「协议文档列出的」：``0x02`` 打成 ``0x20`` 照样被挡下。但白名单仍然不是背书——
    放行一个编码只表示协议文档列出了它，不表示那个赫兹数被核实过。

    出处措辞在 ``RAY-308`` 上调过一次：此前写的是「上游表」并特指维特通用编码表，
    而这七个数其实印在本型号自己的协议文档里。**证据强度上调，但拒绝的理由一个字
    没变**——白名单防的是误写，与那些赫兹数对不对无关。
    """
    try:
        return Bandwidth(value)
    except ValueError:
        raise UnsupportedRegisterError(
            f"带宽编码 0x{int(value):02X} 不在协议文档列出的 0x00-0x06 内。"
            f"已登记：{', '.join(f'{b.name}=0x{b.value:02X}' for b in Bandwidth)}"
            "（标称值载于本型号官方协议文档，本库一档都未实测，见 Bandwidth 文档）。"
        ) from None


class RegisterAccess:
    """一台设备的寄存器读写通道。

    由 :class:`~wt901.device.WT901Device` 持有，通过 ``device.registers`` 访问。
    """

    def __init__(
        self,
        device: WT901Device,
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        read_retries: int = DEFAULT_READ_RETRIES,
        write_delay: float = DEFAULT_WRITE_DELAY,
        save_delay: float = DEFAULT_SAVE_DELAY,
    ) -> None:
        self._device = device
        self.read_timeout = read_timeout
        self.read_retries = read_retries
        self.write_delay = write_delay
        self.save_delay = save_delay

        # 按起始寄存器地址关联响应。同一地址可能有多个等待者（例如两处并发读
        # 同一个寄存器），一次响应把它们全部唤醒。
        self._waiters: dict[int, list[asyncio.Future[RegisterResponse]]] = {}

        # **所有**寄存器事务必须串行，读和写都算。
        #
        # 写的理由早就清楚：两个写交错会变成 解锁/解锁/写/写/保存/保存，那不是
        # 「两次写」而是一次语义不明的操作。
        #
        # 读的理由是真机教的：并发读会让多条 GATT 写同时打到同一个特征上，而
        # bleak 的 CoreBluetooth 后端对同一特征只维护一个待完成写入的 future，
        # 其中一个会永远得不到回调 → 永久挂起。离线测试发现不了，因为
        # MemoryTransport.write 立即返回，不存在「写入未完成」这个状态。
        #
        # 并发读是正常用法（周期轮询、上层并发查询），不该由调用方负责避让。
        self._transaction_lock = asyncio.Lock()

        self._applied: list[_AppliedWrite] = []

    # ----- 接收 -----------------------------------------------------------

    def dispatch(self, response: RegisterResponse) -> None:
        """由设备层在收到 ``0x71`` 帧时调用。

        没人等待的地址直接忽略——设备可能在回应别处发出的读，或是上一次超时后
        迟到的响应。把它们当成错误只会制造噪声。
        """
        waiters = self._waiters.pop(response.start_register, None)
        if not waiters:
            return
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(response)

    # ----- 读 -------------------------------------------------------------

    async def read(self, register: int) -> RegisterResponse:
        """读寄存器，返回**该地址起连续 8 个**寄存器的值。

        这不是接口设计的选择，是协议决定的：一次回帧固定携带 8 个寄存器，见
        :data:`~wt901.protocol.registers.REGISTERS_PER_RESPONSE`。所以读 ``0x3A``
        一次拿到 HX/HY/HZ，读 ``0x51`` 一次拿到 Q0–Q3，读 ``0x7F`` 一次拿全序列号。

        **调用方要自己截取关心的那几个。** 多出来的寄存器是真实数据而不是填充，
        但它们是什么取决于起始地址落在哪——本方法不替调用方猜。
        """
        command = commands.read_register(register)
        last_error: TransportTimeoutError | None = None

        async with self._transaction_lock:
            for _ in range(self.read_retries + 1):
                loop = asyncio.get_running_loop()
                waiter: asyncio.Future[RegisterResponse] = loop.create_future()
                self._waiters.setdefault(register, []).append(waiter)
                try:
                    await self._device.write(command)
                    return await asyncio.wait_for(waiter, self.read_timeout)
                except TimeoutError:
                    last_error = TransportTimeoutError(
                        f"读寄存器 0x{register:02X} 超时（{self.read_timeout}s）"
                    )
                finally:
                    self._discard_waiter(register, waiter)

        assert last_error is not None
        raise last_error

    async def read_value(self, register: int) -> int:
        """读单个寄存器的值。内部仍是一次 8 寄存器的读，其余 7 个被丢弃。"""
        response = await self.read(register)
        return response.value_at(register)

    def _discard_waiter(
        self, register: int, waiter: asyncio.Future[RegisterResponse]
    ) -> None:
        pending = self._waiters.get(register)
        if pending is None:
            return
        if waiter in pending:
            pending.remove(waiter)
        if not pending:
            self._waiters.pop(register, None)

    # ----- 写 -------------------------------------------------------------

    async def write(
        self,
        register: int,
        value: int,
        *,
        persist: bool = True,
        remember: bool = True,
    ) -> None:
        """原子地写一个寄存器：解锁 → 延时 → 写 → 延时 → 保存 → 等 flash 写完。

        最后那个等待不是保险起见：设备在 flash 写入期间对部分寄存器回读出中间
        状态。真机实测 ``save`` 之后立刻读 ``0x64``，4/4 读到 0（见
        :data:`DEFAULT_SAVE_DELAY`）。不等就返回，这个事务就不是原子的——调用方
        拿到控制权时设备还在忙。

        ``persist=False`` 跳过保存**与那个等待**，配置只在本次上电期间有效。重连后的配置重放
        走这条路径——模块保存过的配置本就还在，没必要为此再写一次 flash。

        ``remember=False`` 把这次写入排除在重连重放之外。**动作型寄存器必须用
        它**：重放的语义是「把设备恢复成我配置过的样子」，对配置成立，对动作
        不成立。校准就是动作——重放一次加计校准，等于在重连那一刻的姿态下重做
        一次零位标定（见 :mod:`wt901.calibration`）。
        """
        async with self._transaction_lock:
            await self._device.write(commands.unlock())
            await asyncio.sleep(self.write_delay)
            await self._device.write(commands.write_register(register, value))
            await asyncio.sleep(self.write_delay)
            if persist:
                await self._device.write(commands.save())
                # 等 flash 写完再交还控制权。这个 sleep 必须在事务锁**内**：
                # 设备在 flash 写入期间会对寄存器回读出中间状态，若在锁外等，
                # 并发的读就能挤进这个窗口，拿到一个看着正常却是中间态的值。
                await asyncio.sleep(self.save_delay)

        if remember:
            self._remember(register, value)

    def _remember(self, register: int, value: int) -> None:
        """记下已下发的配置，供重连后重放。同一寄存器只保留最后一次。"""
        self._applied = [
            entry for entry in self._applied if entry.register != register
        ]
        self._applied.append(_AppliedWrite(register=register, value=value))

    @property
    def applied_writes(self) -> tuple[_AppliedWrite, ...]:
        """本次会话已下发的配置，按下发顺序。"""
        return tuple(self._applied)

    async def replay(self) -> None:
        """重连后重放已知配置。

        用 ``persist=False``：这些配置多数已经保存在模块里，重连不会把它们抹掉；
        重放是为了覆盖「写过但未保存」的那部分运行时状态，不该顺带产生额外的
        flash 写入。
        """
        for entry in tuple(self._applied):
            await self.write(entry.register, entry.value, persist=False)

    # ----- 具名配置 -------------------------------------------------------

    async def set_output_rate(self, rate: ReturnRate | int) -> ReturnRate:
        """设置回传速率。出厂默认 10 Hz，采集前必须主动配置。"""
        resolved = _coerce_return_rate(rate)
        await self.write(Register.RRATE, resolved)
        return resolved

    async def set_bandwidth(self, bandwidth: Bandwidth | int) -> Bandwidth:
        """设置抗混叠滤波器带宽。

        **档位的标称频率未经本库实测**，见 :class:`~wt901.protocol.registers.Bandwidth`。
        采样定理要求带宽不超过回传速率的一半，但本库不替调用方检查这件事：两个数
        谁也不是被核实过的量纲，用一个没核实的数去校验另一个没核实的数，只会给出
        一种虚假的安全感。
        """
        resolved = _coerce_bandwidth(bandwidth)
        await self.write(Register.BANDWIDTH, resolved)
        return resolved

    async def set_algorithm(self, algorithm: AlgorithmMode | int) -> AlgorithmMode:
        """设置姿态解算算法。

        **这是配置，不是动作**：它会进 ``applied_writes`` 并在重连后被 ``replay()``
        重新下发，和回传速率、带宽一样。设备重连后仍然是调用方要的那个模式。

        注意它门控着「Z 轴角度归零」（``CALSW`` 写 ``0x0004``）——手册注明该操作
        需先切到 :attr:`AlgorithmMode.SIX_AXIS` 才生效。
        """
        resolved = _coerce_algorithm_mode(algorithm)
        await self.write(Register.ALGORITHM, resolved)
        return resolved

    async def set_mounting(self, mounting: Mounting | int) -> Mounting:
        """设置安装方向。

        **这是配置，不是动作**：与速率、带宽、算法模式一样进 ``applied_writes``，
        重连后由 ``replay()`` 重新下发。

        **即使这次写入可能是幂等的也不要省掉它。** 0（水平）是不是出厂默认，本库
        没有核实过；而无论是不是，省掉它都等于依赖设备残留的配置——模块会被别处用过，
        配置又固化在 flash。它存在的意义是让「一份配置快照完全决定设备状态」成立，
        不是为了改变什么。
        """
        resolved = _coerce_mounting(mounting)
        await self.write(Register.MOUNTING, resolved)
        return resolved

    # ----- 设备级动作（不是配置） -----------------------------------------

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """独占这条 GATT 特征。

        **不属于寄存器事务、但同样要串行化的下行指令**用它——目前只有
        :meth:`~wt901.device.WT901Device.set_bluetooth_name`（那条指令不是寄存器
        写入，封装完全不同）。

        为什么必须串行：所有下行字节都打在**同一条 GATT 写特征**上，而 bleak 的
        CoreBluetooth 后端对同一特征只维护一个待完成写入的 future——两条写同时在飞，
        其中一个永远得不到回调，**永久挂起**。这是真机上确认过的（RAY-177），离线
        测试发现不了，因为 ``MemoryTransport.write`` 立即返回。

        「它不是寄存器写入」不是绕过这把锁的理由：链路不管字节是什么意思。
        """
        async with self._transaction_lock:
            yield

    async def _send_save_action(self, action: SaveAction) -> None:
        """按官方指令流程发一条 ``0x00`` 指令：解锁 → 延时 → 写。

        **末尾不再补一次保存**——``0x00`` 本身就是保存指令。若照 :meth:`write` 的
        三步走，:attr:`SaveAction.RESTORE_DEFAULTS` 之后会立刻跟一条
        ``FF AA 00 00 00``，把刚刚复位掉的配置又存回去一次；:attr:`SaveAction.REBOOT`
        之后那一条则会打在一台正在重启的设备上。这就是为什么这两个动作不能走
        :meth:`write`，而不是「顺手复用一下」。

        走同一把事务锁：这三条指令与寄存器读写抢的是同一条 GATT 特征。
        """
        async with self._transaction_lock:
            await self._device.write(commands.unlock())
            await asyncio.sleep(self.write_delay)
            await self._device.write(commands.save(action))

    async def restore_defaults(self) -> None:
        """**把设备打回出厂配置并保存**（``FF AA 00 01 00``）。

        这不是「撤销我这次的改动」，是抹掉设备 flash 里的**全部**配置——包括别的
        软件写进去的。协议文档给出默认值的两项会回到文档值（回传速率 ``0x06`` =
        10 Hz、带宽 ``0x0004``）；**安装方向与算法的出厂默认值本库没有核实过**
        （见 :attr:`Register.MOUNTING`），所以「复位之后它们是什么」这句话本库答不了
        ——只能说它们不再是你写进去的值。

        **它同时清空本对象的 ``applied_writes``。** 不清的话，下一次自动重连会把
        刚刚被抹掉的配置重新放回去——调用方看到的是「复位了，过一会儿又变回来了」，
        而且没有任何报错。既然设备已回出厂态，「我配置过的样子」这份记录就不再对应
        任何真实状态，留着它只会说谎。

        **复位后设备不再是你配置的样子**，需要重新走一遍配置流程。

        ⚠ **本库没有在真机上验证过这条指令。** 字节构造照协议文档写死并有离线测试，
        但设备收到之后到底做了什么没有实测数据（``ray-308``）。
        """
        await self._send_save_action(SaveAction.RESTORE_DEFAULTS)
        self._applied.clear()

    async def reboot(self) -> None:
        """**重启设备**（``FF AA 00 FF 00``）。

        改完蓝牙名称之后必须重启才生效，见
        :meth:`~wt901.device.WT901Device.set_bluetooth_name`。

        ⚠ **会断开 BLE 链路。** 设备重启期间不可能维持连接，所以本方法**发出即返回**，
        不等待、也无从确认设备是否真的重启了——重启成功与链路断开在观测上是同一件事。
        开着自动重连就会在设备回来后自动接上；否则调用方要自己重连。

        ``applied_writes`` **有意不清空**：重启不改变 flash 里的配置，重连后重放仍然
        是对的。这与 :meth:`restore_defaults` 的区别正是「设备还是不是我配置的样子」。

        ⚠ **本库没有在真机上验证过这条指令。**
        """
        await self._send_save_action(SaveAction.REBOOT)

    async def read_output_rate(self) -> int:
        """读回 ``RRATE`` 的原始编码。

        返回原始 int 而非 :class:`ReturnRate`：设备上可能存着一个本库尚未核实的
        档位（例如上位机软件设过），把它硬塞进枚举会抛异常，而调用方只是想知道
        设备现在是什么状态。
        """
        return await self.read_value(Register.RRATE)

    async def read_bandwidth(self) -> int:
        """读回 ``BANDWIDTH`` 的原始编码。理由同 :meth:`read_output_rate`。"""
        return await self.read_value(Register.BANDWIDTH)

    async def read_algorithm(self) -> int:
        """读回 ``ALGORITHM`` 的原始编码。理由同 :meth:`read_output_rate`。"""
        return await self.read_value(Register.ALGORITHM)

    async def read_mounting(self) -> int:
        """读回 ``MOUNTING`` 的原始编码。理由同 :meth:`read_output_rate`。"""
        return await self.read_value(Register.MOUNTING)

    @asynccontextmanager
    async def settings(self) -> AsyncIterator[Settings]:
        """批量配置。退出 ``async with`` 时统一下发。

        逐项仍按「解锁 → 写 → 保存」的完整时序执行，**不合并成一次解锁多次写**：
        那种批量形式没有在官方资料或真机上得到证实，而寄存器写入出错的表现是
        设备静默进入未知状态。这里选择多花几百毫秒。
        """
        pending = Settings()
        yield pending
        if pending.output_rate is not None:
            await self.set_output_rate(pending.output_rate)
        if pending.bandwidth is not None:
            await self.set_bandwidth(pending.bandwidth)
        if pending.algorithm is not None:
            await self.set_algorithm(pending.algorithm)
        if pending.mounting is not None:
            await self.set_mounting(pending.mounting)
