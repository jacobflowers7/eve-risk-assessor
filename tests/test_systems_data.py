from backend.systems_data import SYSTEMS


def test_systems_have_required_fields():
    assert len(SYSTEMS) > 0
    for system in SYSTEMS:
        assert "system_id" in system
        assert "name" in system
        assert system["region"] in ("Providence", "Catch")


def test_systems_cover_both_regions():
    regions = {s["region"] for s in SYSTEMS}
    assert regions == {"Providence", "Catch"}


def test_no_duplicate_system_ids():
    ids = [s["system_id"] for s in SYSTEMS]
    assert len(ids) == len(set(ids))
