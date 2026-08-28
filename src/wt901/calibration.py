"""加计校准与磁场校准。

两者都通过写寄存器 ``CALSW 0x01`` 实现，但**性质完全不同**：

- 加计校准是一次性动作，写下去就完成了。
- 磁场校准是**有状态的成对操作**：写 ``0x0007`` 进入校准态，绕三轴各转一圈，
  再写 ``0x0000`` 退出。忘记退出会让设备停留在校准态——期间输出的角度不可用于
  测量，而且没有任何报错提示这一点。

所以本模块的主推接口是 :meth:`Calibration.field_calibration` 这个异步上下文
管理器：让「忘记结束」在语法上不可能发生。

**校准写入不参与重连重放**（``remember=False``）。``RegisterAccess`` 会记住写过的
配置，在自动重连后重放，好让设备恢复成调用方配置的样子。那个语义对配置成立，
对动作不成立：

- 重放加计校准 = 在重连那一刻的姿态下重做一次零位标定。设备此时未必水平，
  于是把一个倾斜姿态固化成「水平」——而且不报错，只是从此所有角度都偏。
- 重放「进入磁场校准」= 让设备悄悄回到校准态，而配对的结束调用早已随着那次
  掉线的异常走完了，没有人会再发结束指令。上下文管理器想防的正是这件事。

重连后设备的校准状态因此是**未知**的：本库不重放，也无从查询（``0x01`` 只写）。
需要确定状态就重新校准一次。

``0x01`` 的另外两个官方取值（Z 轴角度归零 ``0x04``、设置角度参考 ``0x08``，RAY-308
补齐）**同样是动作**，同样 ``remember=False``：重放一次归零就是在重连那一刻的朝向上
重定零点，与重放加计校准是同一种错。

⚠ **这两条本库都没有在真机上验证过**：字节构造照协议文档写死并有离线测试，设备收到
之后到底做了什么没有实测数据。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from wt901.errors import ConfigurationError
from wt901.protocol.registers import AlgorithmMode, CalibrationMode, Register

if TYPE_CHECKING:
    from wt901.device import WT901Device

__all__ = ["Calibration"]

_LOGGER = logging.getLogger(__name__)


class Calibration:
    """一台设备的校准通道，通过 ``device.calibration`` 访问。"""

    def __init__(self, device: WT901Device) -> None:
        self._device = device
        self._field_calibrating = False

    @property
    def is_field_calibrating(self) -> bool:
        """设备是否正处于磁场校准态。

        **校准态下的角度输出不可用于测量。** 这个标志让上层能把该时段的样本标记
        为不可信，而不是事后才发现姿态数据有一段是错的。

        注意它只反映**本对象发起的**校准。设备也可能被上位机软件置入校准态，
        那种情况本库无从知晓——寄存器 ``0x01`` 是只写的。
        """
        return self._field_calibrating

    async def calibrate_acceleration(self) -> None:
        """加计校准。

        **前置条件：设备必须水平静置。** 校准过程把当前读数当作零位基准，设备
        在动或没放平时校准，会把那个姿态固化成「水平」，之后所有角度都带这个
        偏差——而且看不出异常，只是一直偏。

        这是一次性动作，不需要配对的结束操作。

        ``remember=False``：校准是动作不是配置，不参与重连重放。见模块文档末尾。
        """
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.ACCELERATION, remember=False
        )

    async def zero_z_axis(self) -> None:
        """把当前朝向定为航向角（Z 轴）零位。

        ⚠ **只在 6 轴算法下生效。** 协议文档：「发送这个指令前需要先切换六轴算法，
        才可以生效。」9 轴模式下航向由磁力计给出绝对参考，没有「归零」可言——**设备
        收到这条指令不会报错，也不会做任何事**。

        所以本方法**先读回 ``0x24`` 确认**，不是 9 轴才发指令。这多花一次 BLE 往返
        （寄存器读与 ``0x61`` 实时流抢同一条链路），换的是把一条静默失效的指令挡在
        外面。本库对这类「不报错但没生效」一贯选择多付代价：`Register.MOUNTING`、
        `AlgorithmMode` 的具名要求都是同一个理由。

        读回的是设备的**真实**状态而不是本库记得的状态——设备可能被上位机软件改过。

        想跳过这次检查，用通用写入::

            await device.registers.write(Register.CALSW, CalibrationMode.ZERO_Z_AXIS,
                                         remember=False)

        ``remember=False``：归零是动作不是配置，不参与重连重放。理由见模块文档末尾
        ——重放一次归零，等于在重连那一刻的朝向上重定零点。

        ⚠ **本库没有在真机上验证过这条指令。** 字节构造照协议文档写死并有离线测试，
        但设备收到之后到底做了什么没有实测数据（``ray-308``）。
        """
        algorithm = await self._device.registers.read_algorithm()
        if algorithm != AlgorithmMode.SIX_AXIS:
            raise ConfigurationError(
                f"Z 轴角度归零只在 6 轴算法下生效，设备当前 ALGORITHM(0x24)="
                f"0x{algorithm:04X}。先 set_algorithm(AlgorithmMode.SIX_AXIS)，"
                "或用 registers.write() 绕过这次检查。"
            )
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.ZERO_Z_AXIS, remember=False
        )

    async def set_angle_reference(self) -> None:
        """把当前姿态定为三轴角度的零点。

        与 :meth:`calibrate_acceleration` 的区别值得说清，两者都「拿当前状态当基准」
        但标定的不是一回事：

        - 加计校准标定**传感器零位**，要求设备**水平静置**，标的是重力方向。
        - 这一条标定**姿态参考**，把当前姿态当零点，**与设备水不水平无关**。

        协议文档注明发送后需再发保存指令；本库的写事务本来就以保存收尾，自动满足。

        ``remember=False``：与其它校准一致，是动作不是配置。

        ⚠ **本库没有在真机上验证过这条指令。**
        """
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.ANGLE_REFERENCE, remember=False
        )

    async def start_field_calibration(self) -> None:
        """进入磁场校准态。

        进入后需**绕 X/Y/Z 三轴各缓慢转一圈**，让磁力计采到各个朝向的样本。
        完成后必须调用 :meth:`end_field_calibration`。

        优先使用 :meth:`field_calibration` 上下文管理器——它保证退出。
        """
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.MAGNETIC_FIELD, remember=False
        )
        self._field_calibrating = True

    async def end_field_calibration(self) -> None:
        """退出磁场校准态，回到正常输出。"""
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.NORMAL, remember=False
        )
        self._field_calibrating = False

    @asynccontextmanager
    async def field_calibration(self) -> AsyncIterator[Calibration]:
        """磁场校准的推荐用法::

            async with device.calibration.field_calibration():
                await rotate_the_sensor_around_all_three_axes()

        进入即开始，退出即结束——**包括 with 体内抛异常的情况**。这不是锦上添花：
        磁场校准没有超时也没有自动退出，一次未捕获的异常就能让设备无限期停在
        校准态，而唯一的症状是姿态数据一直不对。
        """
        await self.start_field_calibration()
        try:
            yield self
        finally:
            # 结束指令必须发出去。这里不吞异常——若连结束都失败，调用方必须知道
            # 设备可能还停在校准态。
            await self.end_field_calibration()

    async def guided_field_calibration(
        self, rotation_seconds: float = 15.0
    ) -> None:
        """按固定时长走完一次磁场校准，期间由操作者转动设备。

        只是 :meth:`field_calibration` 的一个便捷封装，给「脚本里跑一遍」这种
        场景用。时长要够操作者从容转完三轴——转太快磁力计采不到足够朝向。
        """
        _LOGGER.info("磁场校准开始：请在 %.0f 秒内绕 XYZ 三轴各缓慢转一圈", rotation_seconds)
        async with self.field_calibration():
            await asyncio.sleep(rotation_seconds)
        _LOGGER.info("磁场校准结束")
