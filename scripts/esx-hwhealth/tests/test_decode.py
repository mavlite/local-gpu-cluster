import pytest
from hwhealth.decode import tctl_c, ccd_c, rapl_energy_unit_j, energy_delta_j, effective_mhz

def test_tctl_range_select():
    assert tctl_c(0x7d3b0000) == pytest.approx(76.125)
    assert tctl_c(0x7d5b0000) == pytest.approx(76.25)

def test_tctl_without_range_select():
    assert tctl_c(600 << 21) == pytest.approx(75.0)

def test_ccd_valid_and_invalid():
    assert ccd_c(0xb8f) == pytest.approx(64.875)
    assert ccd_c(0xbec) == pytest.approx(76.5)
    assert ccd_c(0x38f) is None          # bit 11 clear

def test_rapl_unit():
    assert rapl_energy_unit_j(0xa1000) == 2 ** -16

def test_energy_delta():
    assert energy_delta_j(0xcaacc12d, 0xcb433a5c, 2 ** -16) == pytest.approx(150.4734, rel=1e-4)

def test_energy_delta_wraps():
    assert energy_delta_j(0xFFFFFFF0, 0x10, 1.0) == 32.0

def test_effective_mhz():
    assert effective_mhz(2495.3, 2000, 1000) == pytest.approx(4990.6)

def test_effective_mhz_zero_mperf():
    assert effective_mhz(2495.3, 5, 0) is None

def test_tctl_tj_sel_both_bits_subtracts_49_without_bit_19():
    # k10temp: CUR_TEMP_TJ_SEL (bits 17:16) == 0b11 also selects the -49 range.
    assert tctl_c((600 << 21) | (0b11 << 16)) == pytest.approx(75.0 - 49)
    assert tctl_c((600 << 21) | (0b01 << 16)) == pytest.approx(75.0)
    assert tctl_c((600 << 21) | (0b10 << 16)) == pytest.approx(75.0)
    # bit 19 and TJ_SEL together subtract only once
    assert tctl_c((600 << 21) | (1 << 19) | (0b11 << 16)) == pytest.approx(75.0 - 49)
