# IronPython 2.7 - appended to build/renode-cf/scripts/monitor.py at launch.
# Exposes: cpu_stats, imu_tick, imu_burst, imu_stream_start/stop/status, baro_tick
#
# IMU stream uses Machine.ClockSource (emulation thread), NOT System.Threading.
# A host thread calling Feed*/TriggerDataInterrupt races Renode and can CPU-abort.

from Antmicro.Renode.Core import EmulationManager
from Antmicro.Renode.Time import ClockEntry
from System import Action
import time

_last_ins = None
_last_virt = None
_last_wall = None

# Clock-driven IMU feeder state
_stream_handler = None
_stream_machine = None
_stream_n = 0
_stream_hz = 0.0
_stream_t0 = 0.0
_stream_virt0 = 0.0
# Mutable sample updated by imu_stream_set without restarting the clock.
# [gx, gy, gz, ax, ay, az, pressure, temp]
_stream_sample = [0.0, 0.0, 0.0, 0.0, 0.0, 1000.0, 101325.0, 25.0]


def mc_cpu_stats():
    global _last_ins, _last_virt, _last_wall

    machine = self.Machine
    if machine is None:
        print "error=no machine selected (run: mach set CF2.1)"
        return

    cpu = machine["sysbus.cpu"]
    ins = long(cpu.ExecutedInstructions)
    mips_cap = int(cpu.PerformanceInMips)
    pc = long(cpu.PC.RawValue)

    master = EmulationManager.Instance.CurrentEmulation.MasterTimeSource
    virt_s = master.ElapsedVirtualTime.TotalSeconds
    host_s = master.ElapsedHostTime.TotalSeconds

    wall = time.time()
    rate = 0.0
    util = 0.0
    sim_speed = 0.0

    if _last_wall is not None:
        dt = wall - _last_wall
        if dt > 0:
            if _last_ins is not None and ins >= _last_ins:
                rate = (ins - _last_ins) / dt
            if _last_virt is not None:
                sim_speed = (virt_s - _last_virt) / dt
                if rate <= 0 and mips_cap > 0:
                    rate = sim_speed * mips_cap * 1000000.0
            if mips_cap > 0 and rate > 0:
                util = min(100.0, 100.0 * rate / (mips_cap * 1000000.0))

    print (
        "ins=%d rate=%.0f util=%.1f%% mips_cap=%d sim_speed=%.3fx "
        "pc=0x%x virt=%.6fs host=%.6fs"
    ) % (ins, rate, util, mips_cap, sim_speed, pc, virt_s, host_s)

    _last_ins = ins
    _last_virt = virt_s
    _last_wall = wall


def mc_cpu_stats_reset():
    global _last_ins, _last_virt, _last_wall
    _last_ins = None
    _last_virt = None
    _last_wall = None
    print "cpu_stats baseline reset"


def _require_machine():
    machine = self.Machine
    if machine is None:
        print "error=no machine selected (run: mach set \"CF2.1\")"
        return None
    return machine


def _virt_seconds():
    master = EmulationManager.Instance.CurrentEmulation.MasterTimeSource
    return master.ElapsedVirtualTime.TotalSeconds


def _imu_feed_one(machine, gx, gy, gz, ax, ay, az):
    gyro = machine["sysbus.i2c3.bmi_gyro"]
    accel = machine["sysbus.i2c3.bmi_accel"]
    gyro.FeedGyroSample(float(gx), float(gy), float(gz), 1)
    accel.FeedAccSample(float(ax), float(ay), float(az), 1)
    gyro.TriggerDataInterrupt()


def mc_imu_tick(gx, gy, gz, ax, ay, az):
    """Feed one gyro+accel sample and pulse gyro DRDY (Int3)."""
    machine = _require_machine()
    if machine is None:
        return
    paused = machine.ObtainPausedState()
    try:
        _imu_feed_one(machine, gx, gy, gz, ax, ay, az)
    finally:
        paused.Dispose()


def mc_imu_burst(n, gx, gy, gz, ax, ay, az):
    """Feed N identical IMU samples + DRDY in one monitor round-trip."""
    machine = _require_machine()
    if machine is None:
        return
    count = int(n)
    if count < 1:
        print "error=n must be >= 1"
        return
    if count > 5000:
        count = 5000
    paused = machine.ObtainPausedState()
    try:
        i = 0
        while i < count:
            _imu_feed_one(machine, gx, gy, gz, ax, ay, az)
            i += 1
    finally:
        paused.Dispose()
    print "fed=%d" % count


def mc_baro_tick(pressure_pa, temp_c):
    """Feed one barometer pressure/temperature sample."""
    machine = _require_machine()
    if machine is None:
        return
    paused = machine.ObtainPausedState()
    try:
        baro = machine["sysbus.i2c3.bmp_baro"]
        baro.FeedPTSample(float(pressure_pa), float(temp_c), 1)
    finally:
        paused.Dispose()


def mc_imu_hover_tick():
    """One rest/hover sample: gyro ~0, accel ~1g on Z, then DRDY."""
    mc_imu_tick(0, 0, 0, 0, 0, 1000)


def _imu_stream_stop_internal():
    global _stream_handler, _stream_machine
    handler = _stream_handler
    machine = _stream_machine
    _stream_handler = None
    _stream_machine = None
    if handler is None or machine is None:
        return False
    try:
        paused = machine.ObtainPausedState()
        try:
            machine.ClockSource.RemoveClockEntry(handler)
        finally:
            paused.Dispose()
    except Exception:
        pass
    return True


def mc_imu_stream_stop():
    """Stop clock-driven IMU feeder."""
    _imu_stream_stop_internal()
    print "stream=stopped n=%d" % _stream_n


def mc_imu_stream_start(hz, gx, gy, gz, ax, ay, az, baro_hz=0, pressure=101325, temp=25):
    """Start IMU feeder on Machine.ClockSource at hz (virtual-time rate)."""
    global _stream_handler, _stream_machine, _stream_n, _stream_hz, _stream_t0, _stream_virt0
    machine = _require_machine()
    if machine is None:
        return
    rate = float(hz)
    if rate <= 0:
        print "error=hz must be > 0"
        return
    if rate > 2000:
        rate = 2000.0

    _imu_stream_stop_internal()

    gyro = machine["sysbus.i2c3.bmi_gyro"]
    accel = machine["sysbus.i2c3.bmi_accel"]
    baro = None
    baro_every = 0
    if float(baro_hz) > 0:
        baro_every = max(1, int(round(rate / float(baro_hz))))
        baro = machine["sysbus.i2c3.bmp_baro"]

    _stream_sample[0] = float(gx)
    _stream_sample[1] = float(gy)
    _stream_sample[2] = float(gz)
    _stream_sample[3] = float(ax)
    _stream_sample[4] = float(ay)
    _stream_sample[5] = float(az)
    _stream_sample[6] = float(pressure)
    _stream_sample[7] = float(temp)

    def on_tick():
        global _stream_n
        try:
            s = _stream_sample
            gyro.FeedGyroSample(s[0], s[1], s[2], 1)
            accel.FeedAccSample(s[3], s[4], s[5], 1)
            gyro.TriggerDataInterrupt()
            n = _stream_n
            if baro is not None and baro_every > 0 and (n % baro_every) == 0:
                baro.FeedPTSample(s[6], s[7], 1)
            _stream_n = n + 1
        except Exception:
            pass

    handler = Action(on_tick)
    # period=1 tick at `rate` Hz virtual time (same pattern as SimpleTicker)
    ratio = ClockEntry.FrequencyToRatio(gyro, long(rate))
    entry = ClockEntry(1, ratio, handler, gyro, "imu_stream")

    paused = machine.ObtainPausedState()
    try:
        machine.ClockSource.AddClockEntry(entry)
    finally:
        paused.Dispose()

    _stream_handler = handler
    _stream_machine = machine
    _stream_n = 0
    _stream_hz = rate
    _stream_t0 = time.time()
    _stream_virt0 = _virt_seconds()
    print "stream=started hz=%.1f baro_hz=%.1f mode=clock" % (rate, float(baro_hz))


def mc_imu_stream_set(gx, gy, gz, ax, ay, az, pressure=101325, temp=25):
    """Update live IMU/baro sample used by the running ClockSource feeder."""
    if _stream_handler is None:
        print "error=stream not running (imu_stream_start first)"
        return
    _stream_sample[0] = float(gx)
    _stream_sample[1] = float(gy)
    _stream_sample[2] = float(gz)
    _stream_sample[3] = float(ax)
    _stream_sample[4] = float(ay)
    _stream_sample[5] = float(az)
    _stream_sample[6] = float(pressure)
    _stream_sample[7] = float(temp)
    print "stream=updated"


def mc_imu_stream_status():
    """Print clock-driven feeder progress (wall + virtual rates)."""
    running = 0
    if _stream_handler is not None:
        running = 1
    elapsed = time.time() - _stream_t0 if _stream_t0 > 0 else 0.0
    out_hz = (_stream_n / elapsed) if elapsed > 0.05 else 0.0
    virt_elapsed = _virt_seconds() - _stream_virt0 if _stream_t0 > 0 else 0.0
    virt_hz = (_stream_n / virt_elapsed) if virt_elapsed > 0.05 else 0.0
    print (
        "streaming=%d n=%d target_hz=%.1f out_hz=%.1f virt_hz=%.1f "
        "elapsed=%.2f virt=%.2f"
    ) % (running, _stream_n, _stream_hz, out_hz, virt_hz, elapsed, virt_elapsed)


# ELF symbols (cf2.elf / sensors_bmi088_bmp3xx.c) - refresh if firmware rebuild moves BSS.
# These are the values for crazyflie-firmware b48aba18. At run time the firmware profile
# (scripts/firmware_profile.py) replaces every address and offset below with the ones of
# the ELF actually loaded.
_GYRO_BIAS_FOUND_ADDR = 0x20006100
_GYRO_BIAS_RUNNING_ADDR = 0x20006104  # BiasObj
_BIAS_IS_FOUND_OFF = 36
_BIAS_IS_FILLED_OFF = 37


def mc_calib_status():
    """Read gyroBiasFound / BiasObj flags from guest RAM (past sensor calib?)."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    bias_found = int(bus.ReadByte(_GYRO_BIAS_FOUND_ADDR))
    # Second byte is the other sensors_*.c copy if linked; BMI088 uses first.
    bias_found_b = int(bus.ReadByte(_GYRO_BIAS_FOUND_ADDR + 1))
    is_found = int(bus.ReadByte(_GYRO_BIAS_RUNNING_ADDR + _BIAS_IS_FOUND_OFF))
    is_filled = int(bus.ReadByte(_GYRO_BIAS_RUNNING_ADDR + _BIAS_IS_FILLED_OFF))
    pc = long(machine["sysbus.cpu"].PC.RawValue)
    calibrated = 1 if (bias_found != 0 or is_found != 0) else 0
    print (
        "calibrated=%d gyroBiasFound=%d/%d isBiasValueFound=%d "
        "isBufferFilled=%d pc=0x%x"
    ) % (calibrated, bias_found, bias_found_b, is_found, is_filled, pc)


# supervisorMem @ 0x2000df00 (cf2.elf DWARF). state is uint8 enum.
_SUPERVISOR_MEM_ADDR = 0x2000DF00
_SUP_OFF_CAN_FLY = 0
_SUP_OFF_IS_FLYING = 1
_SUP_OFF_IS_TUMBLED = 2
_SUP_OFF_IS_ARMED = 3
_SUP_OFF_IS_CRASHED = 4
_SUP_OFF_INFO = 6
_SUP_OFF_STATE = 40
_SUP_OFF_CONDITIONS = 44

_SUP_STATE_NAMES = {
    0: "NotInitialized",
    1: "PreFlChecksNotPassed",
    2: "PreFlChecksPassed",
    3: "Arming",
    4: "ReadyToFly",
    5: "Flying",
    6: "Landed",
    7: "Reset",
    8: "WarningLevelOut",
    9: "ExceptFreeFall",
    10: "Locked",
    11: "Crashed",
}


def mc_preflight_status():
    """Read supervisorMem: preflight / arm / canFly (past pre-flight checks?)."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    base = _SUPERVISOR_MEM_ADDR
    can_fly = int(bus.ReadByte(base + _SUP_OFF_CAN_FLY))
    is_flying = int(bus.ReadByte(base + _SUP_OFF_IS_FLYING))
    is_tumbled = int(bus.ReadByte(base + _SUP_OFF_IS_TUMBLED))
    is_armed = int(bus.ReadByte(base + _SUP_OFF_IS_ARMED))
    is_crashed = int(bus.ReadByte(base + _SUP_OFF_IS_CRASHED))
    info = int(bus.ReadWord(base + _SUP_OFF_INFO))
    state = int(bus.ReadByte(base + _SUP_OFF_STATE))
    cond = long(bus.ReadDoubleWord(base + _SUP_OFF_CONDITIONS))
    name = _SUP_STATE_NAMES.get(state, "Unknown")
    # Past the PreFlChecksNotPassed gate (includes Arming / ReadyToFly / ...)
    preflight_ok = 1 if state in (2, 3, 4, 5, 6, 8) else 0
    tumbled_c = 1 if (cond & (1 << 2)) else 0
    deck_c = 1 if (cond & (1 << 9)) else 0
    print (
        "preflight_ok=%d state=%d(%s) canFly=%d isArmed=%d isFlying=%d "
        "isTumbled=%d isCrashed=%d info=0x%x cond=0x%x tumbled_c=%d deck_c=%d"
    ) % (
        preflight_ok, state, name, can_fly, is_armed, is_flying,
        is_tumbled, is_crashed, info, cond, tumbled_c, deck_c,
    )


# motors.c BSS (cf2.elf) - motor_ratios[4] uint16, motorPowerSet[4], motorSetEnable
_MOTOR_RATIOS_ADDR = 0x200053CE
_MOTOR_POWER_SET_ADDR = 0x200053C4
_MOTOR_SET_ENABLE_ADDR = 0x200053CC
# STM32 TIM CCR: TIM2@0x40000000, TIM4@0x40000800; CCRn @ 0x34+4*(n-1)
_TIM2_BASE = 0x40000000
_TIM4_BASE = 0x40000800
_CCR_OFF = (0, 0x34, 0x38, 0x3C, 0x40)  # 1-indexed


def _read_u16(bus, addr):
    return int(bus.ReadByte(addr)) | (int(bus.ReadByte(addr + 1)) << 8)


def _write_u16(bus, addr, value):
    v = int(value) & 0xFFFF
    bus.WriteByte(addr, v & 0xFF)
    bus.WriteByte(addr + 1, (v >> 8) & 0xFF)


def _ratio_to_ccr_bits(ratio):
    # motorsConv16ToBits: MOTORS_PWM_BITS=8 -> top 8 bits
    return (int(ratio) >> 8) & 0xFF


def mc_motor_stats():
    """Read motor_ratios[m1..m4] (0..65535) and TIM CCR duty bits."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    paused = machine.ObtainPausedState()
    try:
        r = []
        i = 0
        while i < 4:
            r.append(_read_u16(bus, _MOTOR_RATIOS_ADDR + 2 * i))
            i += 1
        # M1=TIM2_CH2, M2=TIM2_CH4, M3=TIM2_CH1, M4=TIM4_CH4
        ccr_m1 = long(bus.ReadDoubleWord(_TIM2_BASE + _CCR_OFF[2])) & 0xFFFF
        ccr_m2 = long(bus.ReadDoubleWord(_TIM2_BASE + _CCR_OFF[4])) & 0xFFFF
        ccr_m3 = long(bus.ReadDoubleWord(_TIM2_BASE + _CCR_OFF[1])) & 0xFFFF
        ccr_m4 = long(bus.ReadDoubleWord(_TIM4_BASE + _CCR_OFF[4])) & 0xFFFF
        enable = int(bus.ReadByte(_MOTOR_SET_ENABLE_ADDR))
    finally:
        paused.Dispose()
    print (
        "m1=%d m2=%d m3=%d m4=%d "
        "ccr_m1=%d ccr_m2=%d ccr_m3=%d ccr_m4=%d enable=%d "
        "n1=%.4f n2=%.4f n3=%.4f n4=%.4f"
    ) % (
        r[0], r[1], r[2], r[3],
        ccr_m1, ccr_m2, ccr_m3, ccr_m4,
        enable,
        r[0] / 65535.0, r[1] / 65535.0, r[2] / 65535.0, r[3] / 65535.0,
    )


def mc_motor_force(m1, m2, m3, m4):
    """Open-loop: set motorPowerSet + motorSetEnable=1 so stabilizer applies ratios."""
    machine = _require_machine()
    if machine is None:
        return
    vals = [int(float(m1)), int(float(m2)), int(float(m3)), int(float(m4))]
    i = 0
    while i < 4:
        if vals[i] < 0:
            vals[i] = 0
        if vals[i] > 65535:
            vals[i] = 65535
        i += 1
    bus = machine.SystemBus
    paused = machine.ObtainPausedState()
    try:
        i = 0
        while i < 4:
            _write_u16(bus, _MOTOR_POWER_SET_ADDR + 2 * i, vals[i])
            _write_u16(bus, _MOTOR_RATIOS_ADDR + 2 * i, vals[i])
            i += 1
        bus.WriteByte(_MOTOR_SET_ENABLE_ADDR, 1)
        # Mirror into TIM CCR (brushed 8-bit PWM)
        bus.WriteDoubleWord(_TIM2_BASE + _CCR_OFF[2], _ratio_to_ccr_bits(vals[0]))  # M1 CH2
        bus.WriteDoubleWord(_TIM2_BASE + _CCR_OFF[4], _ratio_to_ccr_bits(vals[1]))  # M2 CH4
        bus.WriteDoubleWord(_TIM2_BASE + _CCR_OFF[1], _ratio_to_ccr_bits(vals[2]))  # M3 CH1
        bus.WriteDoubleWord(_TIM4_BASE + _CCR_OFF[4], _ratio_to_ccr_bits(vals[3]))  # M4 CH4
    finally:
        paused.Dispose()
    print "forced m1=%d m2=%d m3=%d m4=%d enable=1" % (vals[0], vals[1], vals[2], vals[3])


def mc_motor_force_clear():
    """Zero motorPowerSet/ratios/CCR and clear motorSetEnable."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    paused = machine.ObtainPausedState()
    try:
        i = 0
        while i < 4:
            _write_u16(bus, _MOTOR_POWER_SET_ADDR + 2 * i, 0)
            _write_u16(bus, _MOTOR_RATIOS_ADDR + 2 * i, 0)
            i += 1
        bus.WriteByte(_MOTOR_SET_ENABLE_ADDR, 0)
        bus.WriteDoubleWord(_TIM2_BASE + _CCR_OFF[1], 0)
        bus.WriteDoubleWord(_TIM2_BASE + _CCR_OFF[2], 0)
        bus.WriteDoubleWord(_TIM2_BASE + _CCR_OFF[4], 0)
        bus.WriteDoubleWord(_TIM4_BASE + _CCR_OFF[4], 0)
    finally:
        paused.Dispose()
    print "force_cleared enable=0"


# ---------------------------------------------------------------------------
# Setpoint injection (Option C): overwrite FreeRTOS setpointQueue storage.
# sizeof(setpoint_t)=0x80; queue length 1 @ CCM. Refresh timestamp from xTickCount
# so supervisor commander WDT (~500 ms warn / ~2 s shutdown) stays happy.
# Layout assumes -fshort-enums (verified via DWARF / nm size on cf2.elf).
# ---------------------------------------------------------------------------
_SETPOINT_QUEUE_STORAGE = 0x10004270  # osSys_setpointQueueStorage
_PRIORITY_QUEUE_STORAGE = 0x10004340  # osSys_priorityQueueStorage
_XTICK_COUNT_ADDR = 0x20018E80        # xTickCount
_STABILIZER_SETPOINT = 0x2000DDC0     # stabilizer.c static setpoint (debug mirror)
_COMMANDER_PRIORITY_CRTP = 2
_COMMANDER_PRIORITY_DISABLE = 0
_SETPOINT_SIZE = 0x80
# Field offsets inside setpoint_t
_SP_OFF_TIMESTAMP = 0
_SP_OFF_ATT_ROLL = 8
_SP_OFF_ATT_PITCH = 12
_SP_OFF_ATT_YAW = 16
_SP_OFF_RATE_YAW = 32
_SP_OFF_THRUST = 52
# point_t / velocity_t include a timestamp word; x/y/z follow (DWARF on cf2.elf)
_SP_OFF_POS_X = 60
_SP_OFF_POS_Y = 64
_SP_OFF_POS_Z = 68
_SP_OFF_VEL_X = 76
_SP_OFF_VEL_Y = 80
_SP_OFF_VEL_Z = 84
_SP_OFF_VEL_BODY = 120
_SP_OFF_MODE_X = 121
_SP_OFF_MODE_Y = 122
_SP_OFF_MODE_Z = 123
_SP_OFF_MODE_ROLL = 124
_SP_OFF_MODE_PITCH = 125
_SP_OFF_MODE_YAW = 126
# stab_mode_t
_MODE_DISABLE = 0
_MODE_ABS = 1
_MODE_VELOCITY = 2


def _write_f32(bus, addr, value):
    from System import BitConverter, Single
    b = BitConverter.GetBytes(Single(float(value)))
    i = 0
    while i < 4:
        bus.WriteByte(addr + i, int(b[i]) & 0xFF)
        i += 1


def _read_f32(bus, addr):
    """Decode little-endian float32 via BitConverter (IronPython-safe)."""
    from System import Array, BitConverter, Byte
    b = Array.CreateInstance(Byte, 4)
    b[0] = bus.ReadByte(addr)
    b[1] = bus.ReadByte(addr + 1)
    b[2] = bus.ReadByte(addr + 2)
    b[3] = bus.ReadByte(addr + 3)
    return float(BitConverter.ToSingle(b, 0))


def _zero_setpoint_blob(bus, base):
    i = 0
    while i < _SETPOINT_SIZE:
        bus.WriteByte(base + i, 0)
        i += 1


def _pack_rpyt_setpoint(bus, base, roll, pitch, yaw_rate, thrust, tick):
    """Legacy RPYT-style: roll/pitch abs deg, yaw rate deg/s, thrust 0..65535."""
    _zero_setpoint_blob(bus, base)
    bus.WriteDoubleWord(base + _SP_OFF_TIMESTAMP, long(tick) & 0xFFFFFFFF)
    _write_f32(bus, base + _SP_OFF_ATT_ROLL, roll)
    _write_f32(bus, base + _SP_OFF_ATT_PITCH, pitch)
    _write_f32(bus, base + _SP_OFF_ATT_YAW, 0.0)
    _write_f32(bus, base + _SP_OFF_RATE_YAW, -float(yaw_rate))  # legacy inverted
    thr = float(thrust)
    if thr < 0:
        thr = 0.0
    if thr > 60000.0:
        thr = 60000.0
    _write_f32(bus, base + _SP_OFF_THRUST, thr)
    bus.WriteByte(base + _SP_OFF_VEL_BODY, 0)
    bus.WriteByte(base + _SP_OFF_MODE_X, _MODE_DISABLE)
    bus.WriteByte(base + _SP_OFF_MODE_Y, _MODE_DISABLE)
    bus.WriteByte(base + _SP_OFF_MODE_Z, _MODE_DISABLE)
    bus.WriteByte(base + _SP_OFF_MODE_ROLL, _MODE_ABS)
    bus.WriteByte(base + _SP_OFF_MODE_PITCH, _MODE_ABS)
    bus.WriteByte(base + _SP_OFF_MODE_YAW, _MODE_VELOCITY)


def mc_setpoint_rpyt(roll, pitch, yaw, thrust):
    """Inject RPYT setpoint into commander queue (keeps WDT alive)."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    tick = 0
    paused = machine.ObtainPausedState()
    try:
        tick = long(bus.ReadDoubleWord(_XTICK_COUNT_ADDR))
        _pack_rpyt_setpoint(
            bus, _SETPOINT_QUEUE_STORAGE,
            float(roll), float(pitch), float(yaw), float(thrust), tick,
        )
        # Mirror into stabilizer setpoint for observability
        _pack_rpyt_setpoint(
            bus, _STABILIZER_SETPOINT,
            float(roll), float(pitch), float(yaw), float(thrust), tick,
        )
        bus.WriteDoubleWord(_PRIORITY_QUEUE_STORAGE, _COMMANDER_PRIORITY_CRTP)
        # lastUpdate is overwritten by commanderGetSetpoint from setpoint.timestamp
    finally:
        paused.Dispose()
    print (
        "setpoint_rpyt roll=%.2f pitch=%.2f yaw=%.2f thrust=%.0f prio=%d tick=%d"
    ) % (float(roll), float(pitch), float(yaw), float(thrust),
         _COMMANDER_PRIORITY_CRTP, tick)


def _pack_zdist_setpoint(bus, base, roll, pitch, yaw_rate, z_m, tick):
    """
    CRTP zDistance-style: absolute height + attitude.
    mode.z=Abs -> FW position PID owns collective thrust; attitude mixer owns m1..m4.
    """
    _zero_setpoint_blob(bus, base)
    bus.WriteDoubleWord(base + _SP_OFF_TIMESTAMP, long(tick) & 0xFFFFFFFF)
    _write_f32(bus, base + _SP_OFF_ATT_ROLL, float(roll))
    _write_f32(bus, base + _SP_OFF_ATT_PITCH, float(pitch))
    _write_f32(bus, base + _SP_OFF_ATT_YAW, 0.0)
    _write_f32(bus, base + _SP_OFF_RATE_YAW, -float(yaw_rate))
    _write_f32(bus, base + _SP_OFF_THRUST, 0.0)  # unused when mode.z != Disable
    _write_f32(bus, base + _SP_OFF_POS_X, 0.0)
    _write_f32(bus, base + _SP_OFF_POS_Y, 0.0)
    _write_f32(bus, base + _SP_OFF_POS_Z, float(z_m))
    bus.WriteByte(base + _SP_OFF_VEL_BODY, 0)
    bus.WriteByte(base + _SP_OFF_MODE_X, _MODE_DISABLE)
    bus.WriteByte(base + _SP_OFF_MODE_Y, _MODE_DISABLE)
    bus.WriteByte(base + _SP_OFF_MODE_Z, _MODE_ABS)
    bus.WriteByte(base + _SP_OFF_MODE_ROLL, _MODE_ABS)
    bus.WriteByte(base + _SP_OFF_MODE_PITCH, _MODE_ABS)
    bus.WriteByte(base + _SP_OFF_MODE_YAW, _MODE_VELOCITY)


def mc_setpoint_zdist(roll, pitch, yaw, z):
    """Inject absolute-Z + attitude setpoint (FW altitude hold via baro estimate)."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    tick = 0
    paused = machine.ObtainPausedState()
    try:
        tick = long(bus.ReadDoubleWord(_XTICK_COUNT_ADDR))
        _pack_zdist_setpoint(
            bus, _SETPOINT_QUEUE_STORAGE,
            float(roll), float(pitch), float(yaw), float(z), tick,
        )
        _pack_zdist_setpoint(
            bus, _STABILIZER_SETPOINT,
            float(roll), float(pitch), float(yaw), float(z), tick,
        )
        bus.WriteDoubleWord(_PRIORITY_QUEUE_STORAGE, _COMMANDER_PRIORITY_CRTP)
    finally:
        paused.Dispose()
    print (
        "setpoint_zdist roll=%.2f pitch=%.2f yaw=%.2f z=%.3f prio=%d tick=%d"
    ) % (float(roll), float(pitch), float(yaw), float(z),
         _COMMANDER_PRIORITY_CRTP, tick)


def mc_setpoint_hover(thrust=35000):
    """Level attitude + fixed thrust (hover-ish open attitude)."""
    mc_setpoint_rpyt(0, 0, 0, thrust)


def mc_setpoint_clear():
    """Null setpoint + disable priority (motors should idle/cut)."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    paused = machine.ObtainPausedState()
    try:
        tick = long(bus.ReadDoubleWord(_XTICK_COUNT_ADDR))
        _zero_setpoint_blob(bus, _SETPOINT_QUEUE_STORAGE)
        bus.WriteDoubleWord(_SETPOINT_QUEUE_STORAGE + _SP_OFF_TIMESTAMP, tick)
        _zero_setpoint_blob(bus, _STABILIZER_SETPOINT)
        bus.WriteDoubleWord(_STABILIZER_SETPOINT + _SP_OFF_TIMESTAMP, tick)
        bus.WriteDoubleWord(_PRIORITY_QUEUE_STORAGE, _COMMANDER_PRIORITY_DISABLE)
    finally:
        paused.Dispose()
    print "setpoint_cleared"


def mc_setpoint_stats():
    """Read injected/active setpoint from queue storage + tick age."""
    machine = _require_machine()
    if machine is None:
        return
    bus = machine.SystemBus
    paused = machine.ObtainPausedState()
    try:
        base = _SETPOINT_QUEUE_STORAGE
        ts = long(bus.ReadDoubleWord(base + _SP_OFF_TIMESTAMP))
        tick = long(bus.ReadDoubleWord(_XTICK_COUNT_ADDR))
        thr = _read_f32(bus, base + _SP_OFF_THRUST)
        roll = _read_f32(bus, base + _SP_OFF_ATT_ROLL)
        pitch = _read_f32(bus, base + _SP_OFF_ATT_PITCH)
        yaw_rate = _read_f32(bus, base + _SP_OFF_RATE_YAW)
        z = _read_f32(bus, base + _SP_OFF_POS_Z)
        mx = int(bus.ReadByte(base + _SP_OFF_MODE_X))
        my = int(bus.ReadByte(base + _SP_OFF_MODE_Y))
        mz = int(bus.ReadByte(base + _SP_OFF_MODE_Z))
        mr = int(bus.ReadByte(base + _SP_OFF_MODE_ROLL))
        mp = int(bus.ReadByte(base + _SP_OFF_MODE_PITCH))
        myaw = int(bus.ReadByte(base + _SP_OFF_MODE_YAW))
        prio = long(bus.ReadDoubleWord(_PRIORITY_QUEUE_STORAGE))
        age = tick - ts if tick >= ts else 0
    finally:
        paused.Dispose()
    print (
        "thrust=%.0f roll=%.2f pitch=%.2f yaw_rate=%.2f z=%.3f "
        "mode=xy%d%d z%d rpy%d%d%d prio=%d ts=%d tick=%d age=%d"
    ) % (thr, roll, pitch, yaw_rate, z, mx, my, mz, mr, mp, myaw, prio, ts, tick, age)
