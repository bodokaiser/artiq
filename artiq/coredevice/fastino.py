"""RTIO driver for the Fastino 32-channel, 16-bit, 2.5 MS/s per channel
streaming DAC.
"""
from numpy import int32, int64

from artiq.language.core import compile, kernel, portable, KernelInvariant
from artiq.coredevice.rtio import (rtio_output, rtio_output_wide,
                                   rtio_input_data)
from artiq.language.units import ns
from artiq.coredevice.core import Core


@compile
class Fastino:
    """Fastino 32-channel, 16-bit, 2.5 MS/s per channel streaming DAC

    The RTIO PHY supports staging DAC data before transmission by writing
    to the DAC RTIO addresses. If a channel is not "held" by setting its bit
    using :meth:`set_hold`, the next frame will contain the update. For the
    held DACs, updates are triggered explicitly by setting the corresponding
    bit using :meth:`update`. Update is self-clearing. This enables atomic
    DAC updates synchronized to a frame edge.

    The ``log2_width=0`` RTIO layout uses one DAC channel per RTIO address and a
    dense RTIO address space. The RTIO words are narrow (32-bit) and
    few-channel updates are efficient. There is the least amount of DAC state
    tracking in kernels, at the cost of more DMA and RTIO data.
    The setting here and in the RTIO PHY (gateware) must match.

    Other ``log2_width`` (up to ``log2_width=5``) settings pack multiple
    (in powers of two, up to 32) DAC channels into one group and into
    one RTIO write. The RTIO data width increases accordingly. The ``log2_width``
    LSBs of the RTIO address for a DAC channel write must be zero and the
    address space is sparse. For ``log2_width=5`` the RTIO data is 512-bit wide,
    32 channels x 16 bits.

    If ``log2_width`` is zero, the :meth:`set_dac`/:meth:`set_dac_mu` interface
    must be used. If non-zero, the :meth:`set_group`/:meth:`set_group_mu`
    interface must be used.

    :param channel: RTIO channel number
    :param core_device: Core device name (default: "core")
    :param log2_width: Width of DAC channel group (logarithm base 2).
        Value must match the corresponding value in the RTIO PHY (gateware).
    """

    core: KernelInvariant[Core]
    channel: KernelInvariant[int32]
    width: KernelInvariant[int32]
    t_frame: KernelInvariant[int64]

    def __init__(self, dmgr, channel, core_device="core", log2_width=0):
        self.channel = channel << 8
        self.core = dmgr.get(core_device)
        self.width = 1 << log2_width
        # frame duration in mu (14 words each 7 clock cycles each 4 ns)
        # self.core.seconds_to_mu(14*7*4*ns)  # unfortunately this may round wrong
        assert self.core.ref_period == 1*ns
        self.t_frame = int64(14*7*4)

    @staticmethod
    def get_rtio_channels(channel, **kwargs):
        return [(channel, None)]

    @kernel
    def init(self):
        """Initialize the device.

            * disables RESET, DAC_CLR, enables AFE_PWR
            * clears error counters, enables error counting
            * turns LEDs off
            * clears ``hold`` and ``continuous`` on all channels
            * clear and resets interpolators to unit rate change on all
              channels

        It does not change set channel voltages and does not reset the PLLs or clock
        domains.

        .. warning::
            On Fastino gateware before v0.2 this may lead to 0 voltage being emitted
            transiently.
        """
        self.set_cfg(reset=False, afe_power_down=False, dac_clr=False, clr_err=True)
        delay_mu(self.t_frame)
        self.set_cfg(reset=False, afe_power_down=False, dac_clr=False, clr_err=False)
        delay_mu(self.t_frame)
        self.set_continuous(0)
        delay_mu(self.t_frame)
        self.stage_cic(1)
        delay_mu(self.t_frame)
        self.apply_cic(int32(int64(0xffffffff)))
        delay_mu(self.t_frame)
        self.set_leds(0)
        delay_mu(self.t_frame)
        self.set_hold(0)
        delay_mu(self.t_frame)

    @kernel
    def write(self, addr: int32, data: int32):
        """Write data to a Fastino register.

        :param addr: Address to write to.
        :param data: Data to write.
        """
        rtio_output(self.channel | addr, data)

    @kernel
    def read(self, addr: int32):
        """Read from Fastino register.

        TODO: untested

        :param addr: Address to read from.
        :return: The data read.
        """
        raise NotImplementedError
        # rtio_output(self.channel | addr | 0x80)
        # return rtio_input_data(self.channel >> 8)

    @kernel
    def set_dac_mu(self, dac: int32, data: int32):
        """Write DAC data in machine units.

        :param dac: DAC channel to write to (0-31).
        :param data: DAC word to write, 16-bit unsigned integer, in machine
            units.
        """
        self.write(dac, data)

    @kernel
    def set_group_mu(self, dac: int32, data: list[int32]):
        """Write a group of DAC channels in machine units. To specify the
        group, use the index of the first channel in group; e.g., with
        ``log2_width = 4``, reference the first group by channel 0, second by
        channel 16.

        Each entry in ``data`` packs two DAC data words (16-bit unsigned)
        to be written. If the total number of data words is less than the group
        size, the remaining DAC channels within the group are cleared to 0 (machine
        units). Data in excess of the group size is ignored.

        :param dac: First channel in DAC channel group. LSBs up to
            ``log2_width`` must be zero.
        :param data: List of DAC data pairs (2x16-bit unsigned) to write,
            in machine units.
        """
        if dac & (self.width - 1) != 0:
            raise ValueError("Group index LSBs must be zero")
        rtio_output_wide(self.channel | dac, data)

    @portable
    def voltage_to_mu(self, voltage: float) -> int32:
        """Convert SI volts to DAC machine units.

        :param voltage: Voltage in SI volts.
        :return: DAC data word in machine units, 16-bit integer.
        """
        data = int32(round((float(0x8000)/10.)*voltage)) + int32(0x8000)
        if data < 0 or data > 0xffff:
            raise ValueError("DAC voltage out of bounds")
        return data

    @portable
    def voltage_group_to_mu(self, voltage: list[float], data: list[int32]):
        """Convert SI volts to packed DAC channel group machine units.

        :param voltage: List of SI volt voltages.
        :param data: List of DAC channel data pairs to write to.
            Half the length of `voltage`.
        """
        for i in range(len(voltage)):
            v = self.voltage_to_mu(voltage[i])
            if i & 1 != 0:
                v = data[i // 2] | (v << 16)
            data[i // 2] = int32(v)

    @kernel
    def set_dac(self, dac: int32, voltage: float):
        """Set DAC data to given voltage.

        :param dac: DAC channel (0-31).
        :param voltage: Desired output voltage.
        """
        self.write(dac, self.voltage_to_mu(voltage))

    @kernel
    def set_group(self, dac: int32, voltage: list[float]):
        """Set DAC group data to given voltage. To specify the
        group, use the index of the first channel in group; e.g.,
        with ``log2_width = 4``, reference the first group by channel 0,
        second by channel 16.

        If voltage list length is less than the group size, the remaining DAC
        channels within the group are cleared to 0 (machine units). List
        entries in excess of the group size are ignored.

        :param dac: First channel in DAC channel group. LSBs up to
            ``log2_width`` must be zero.
        :param voltage: List of desired output voltages.
        """
        data = [int32(0) for _ in range(len(voltage) // 2)]
        self.voltage_group_to_mu(voltage, data)
        self.set_group_mu(dac, data)

    @kernel
    def update(self, update: int32):
        """Schedule channels for update.

        :param update: Bit mask of channels to update (32-bit).
        """
        self.write(0x20, update)

    @kernel
    def set_hold(self, hold: int32):
        """Set channels to manual update.

        :param hold: Bit mask of channels to hold (32-bit).
        """
        self.write(0x21, hold)

    @kernel
    def set_cfg(self, reset: bool = False, afe_power_down: bool = False, dac_clr: bool = False, clr_err: bool = False):
        """Set configuration bits.

        :param reset: Reset SPI PLL and SPI clock domain.
        :param afe_power_down: Disable AFE power.
        :param dac_clr: Assert all 32 DAC_CLR signals setting all DACs to
            mid-scale (0 V).
        :param clr_err: Clear error counters and PLL reset indicator.
            This clears the sticky red error LED. Must be cleared to enable
            error counting.
        """
        self.write(0x22, (int32(reset) << 0) | (int32(afe_power_down) << 1) |
                   (int32(dac_clr) << 2) | (int32(clr_err) << 3))

    @kernel
    def set_leds(self, leds: int32):
        """Set the green user-defined LEDs.

        :param leds: LED status, 8-bit integer each bit corresponding to one
            green LED.
        """
        self.write(0x23, leds)

    @kernel
    def set_continuous(self, channel_mask: int32):
        """Enable continuous DAC updates on channels regardless of new data
        being submitted.
        """
        self.write(0x25, channel_mask)

    @kernel
    def stage_cic_mu(self, rate_mantissa: int32, rate_exponent: int32, gain_exponent: int32):
        """Stage machine unit CIC interpolator configuration.
        """
        if rate_mantissa < 0 or rate_mantissa >= 1 << 6:
            raise ValueError("rate_mantissa out of bounds")
        if rate_exponent < 0 or rate_exponent >= 1 << 4:
            raise ValueError("rate_exponent out of bounds")
        if gain_exponent < 0 or gain_exponent >= 1 << 6:
            raise ValueError("gain_exponent out of bounds")
        config = rate_mantissa | (rate_exponent << 6) | (gain_exponent << 10)
        self.write(0x26, config)

    @kernel
    def stage_cic(self, rate: int32) -> int32:
        """Compute and stage interpolator configuration.

        This method approximates the desired interpolation rate using a 10-bit
        floating point representation (6-bit mantissa, 4-bit exponent) and
        then determines an optimal interpolation gain compensation exponent
        to avoid clipping. Gains for rates that are powers of two are accurately
        compensated. Other rates lead to overall less than unity gain (but more
        than 0.5 gain).

        The overall gain including gain compensation is ``actual_rate ** order /
        2 ** ceil(log2(actual_rate ** order))``
        where ``order = 3``.

        Returns the actual interpolation rate.
        """
        if rate <= 0 or rate > 1 << 16:
            raise ValueError("rate out of bounds")
        rate_mantissa = rate
        rate_exponent = 0
        while rate_mantissa > 1 << 6:
            rate_exponent += 1
            rate_mantissa >>= 1
        order = 3
        gain = 1
        for i in range(order):
            gain *= rate_mantissa
        gain_exponent = 0
        while gain > 1 << gain_exponent:
            gain_exponent += 1
        gain_exponent += order*rate_exponent
        assert gain_exponent <= order*16
        self.stage_cic_mu(rate_mantissa - 1, rate_exponent, gain_exponent)
        return rate_mantissa << rate_exponent

    @kernel
    def apply_cic(self, channel_mask: int32):
        """Apply the staged interpolator configuration on the specified channels.

        Each Fastino channel starting with gateware v0.2 includes a fourth order
        (cubic) CIC interpolator with variable rate change and variable output
        gain compensation (see :meth:`stage_cic`).

        Fastino gateware before v0.2 does not include the interpolators and the
        methods affecting the CICs should not be used.

        Channels using non-unity interpolation rate should have
        continous DAC updates enabled (see :meth:`set_continuous`) unless
        their output is supposed to be constant.

        This method resets and settles the affected interpolators. There will be
        no output updates for the next ``order = 3`` input samples.
        Affected channels will only accept one input sample per input sample
        period. This method synchronizes the input sample period to the current
        frame on the affected channels.

        If application of new interpolator settings results in a change of the
        overall gain, there will be a corresponding output step.
        """
        self.write(0x27, channel_mask)
