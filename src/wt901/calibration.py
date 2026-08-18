"""加计校准与磁场校准。

两者都通过写寄存器 ``CALSW 0x01`` 实现，但**性质完全不同**：

- 加计校准是一次性动作，写下去就完成了。
- 磁场校准是**有状态的成对操作**：写 ``0x0007`` 进入校准态，绕三轴各转一圈，
  再写 ``0x0000`` 退出。忘记退出会让设备停留在校准态——期间输出的角度不可用于
  测量，而且没有任何报错提示这一点。

所以本模块的主推接口是 :meth:`Calibration.field_calibration` 这个异步上下文
管理器：让「忘记结束」在语法上不可能发生。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from wt901.protocol.registers import CalibrationMode, Register

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
        """
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.ACCELERATION
        )

    async def start_field_calibration(self) -> None:
        """进入磁场校准态。

        进入后需**绕 X/Y/Z 三轴各缓慢转一圈**，让磁力计采到各个朝向的样本。
        完成后必须调用 :meth:`end_field_calibration`。

        优先使用 :meth:`field_calibration` 上下文管理器——它保证退出。
        """
        await self._device.registers.write(
            Register.CALSW, CalibrationMode.MAGNETIC_FIELD
        )
        self._field_calibrating = True

    async def end_field_calibration(self) -> None:
        """退出磁场校准态，回到正常输出。"""
        await self._device.registers.write(Register.CALSW, CalibrationMode.NORMAL)
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
