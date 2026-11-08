from fdmcore.slicer_cli import _condition

MACHINE = {"printer_model": "Bambu Lab X1 Carbon", "nozzle_diameter": ["0.4"], "printer_notes": "PRINTER_VENDOR_BAMBU"}


def test_profile_conditions_without_eval():
    assert _condition('printer_model=~/.*X1.*/ and nozzle_diameter[0]==0.4', MACHINE)
    assert _condition('printer_model=~/.*X1.*/ && nozzle_diameter[0]==0.4', MACHINE)
    assert _condition('!(printer_model=~/.*P1P.*/) || nozzle_diameter[0]>0.6', MACHINE)
    assert _condition('printer_notes!~/PRUSA/ and not printer_model=="Bambu Lab A1"', MACHINE)
    assert not _condition('nozzle_diameter[0]==0.6', MACHINE)


def test_unreadable_conditions_count_as_not_compatible():
    for expr in ("", "unknown_key==1", "__import__('os')", "printer_model=~", "(nozzle_diameter[0]==0.4"):
        assert not _condition(expr, MACHINE)
