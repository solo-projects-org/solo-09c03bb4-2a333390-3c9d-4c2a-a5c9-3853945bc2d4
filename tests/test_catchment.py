"""Service level: catchment generation, incremental re-survey and export."""

import csv
import io

import pytest

from app.polygon import catchment_wkt

DIRECTIONS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _wkt_for(service, grid_id: int, outfall: tuple[int, int]) -> str | None:
    rows = {
        (row["outfall_row"], row["outfall_col"]): row["wkt"]
        for row in service.store.catchment_rows(grid_id)
    }
    return rows.get(outfall)


def _exported_outfalls(service, grid_id: int) -> set[tuple[int, int]]:
    return {
        (row["outfall_row"], row["outfall_col"])
        for row in service.store.catchment_rows(grid_id)
    }


def test_import_generates_one_catchment_row_per_supplied_outfall(service):
    # (0,0)->(0,1)->(0,2) outfall; (1,1) S -> outfall (2,1).
    grid = [
        ["E", "E", "O"],
        ["O", "S", "O"],
        ["O", "O", "O"],
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    rows = service.catchment_export(grid_id)
    assert len(rows) == 2
    by_outfall = {(r["outfall_row"], r["outfall_col"]): r for r in rows}
    top = by_outfall[(0, 2)]
    bottom = by_outfall[(2, 1)]
    assert top["cell_count"] == 2
    assert bottom["cell_count"] == 1
    assert top["wkt"].startswith("MULTIPOLYGON")
    # The geometry matches the supply cell set exactly.
    assert top["wkt"] == catchment_wkt({(0, 0), (0, 1)})


def test_outfall_without_supply_is_not_exported(service):
    # (1,1) N -> outfall (0,1); outfall (0,0) exists but nothing drains to it.
    grid_id = service.import_grid([["O", "O"], ["O", "N"]])["grid_id"]
    assert _exported_outfalls(service, grid_id) == {(0, 1)}


def test_csv_export_has_one_row_per_outfall_and_rfc4180_shape(service):
    grid = [
        ["E", "E", "O"],
        ["O", "S", "O"],
        ["O", "O", "O"],
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    text = service.catchment_csv(grid_id)
    reader = csv.reader(io.StringIO(text))
    header, *data = list(reader)
    assert header == ["grid_id", "outfall_row", "outfall_col", "cell_count", "wkt"]
    assert len(data) == 2
    for record in data:
        assert int(record[0]) == grid_id
        assert record[4].startswith("MULTIPOLYGON")
    assert text.endswith("\r\n")


def test_resurvey_regenerates_only_outfalls_whose_supply_set_changed(service):
    # Every cell eventually reaches the single outfall (2,2).
    grid = [
        ["S", "S", "O"],
        ["S", "S", "O"],
        ["E", "E", "O"],
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    wkt_before = _wkt_for(service, grid_id, (2, 2))
    hash_before = service.store.catchment_hashes(grid_id)[(2, 2)]

    # (1,0) used to go S -> (2,0) -> (2,1) -> outfall.  Re-aim it E so it goes
    # (1,1) -> (2,1) instead: the path changes but the outfall's supply *set*
    # is identical, so the catchment must not be regenerated.
    response = service.resurvey(grid_id, [{"row": 1, "col": 0, "value": "E"}])
    transition = response["catchment_transition"]
    assert transition["regenerated"] == []
    assert {tuple(d.values()) for d in transition["unchanged"]} == {(2, 2)}
    assert transition["removed"] == []
    assert service.store.catchment_hashes(grid_id)[(2, 2)] == hash_before
    assert _wkt_for(service, grid_id, (2, 2)) == wkt_before

    # Now reroute (2,0) north into the grid: it still reaches the outfall via
    # (1,0) E -> (1,1) S -> (2,1), so the supply set stays identical again.
    response = service.resurvey(grid_id, [{"row": 2, "col": 0, "value": "N"}])
    assert response["catchment_transition"]["regenerated"] == []
    assert _wkt_for(service, grid_id, (2, 2)) == wkt_before


def test_resurvey_regenerates_when_membership_changes_even_if_size_kept(service):
    # Two separate one-cell supplies; swap which outfall each cell reaches.
    grid = [
        ["E", "O"],
        ["O", "W"],
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    response = service.resurvey(
        grid_id,
        [
            {"row": 0, "col": 0, "value": "S"},
            {"row": 1, "col": 1, "value": "N"},
        ],
    )
    regenerated = {tuple(d.values()) for d in response["catchment_transition"]["regenerated"]}
    assert regenerated == {(0, 1), (1, 0)}
    assert _wkt_for(service, grid_id, (0, 1)) == catchment_wkt({(1, 1)})
    assert _wkt_for(service, grid_id, (1, 0)) == catchment_wkt({(0, 0)})


def test_unrelated_outfall_catchment_is_left_untouched(service):
    # Two independent chains separated by outfalls; touching the left chain
    # leaves the right outfall's row byte-identical.
    grid = [
        ["E", "O", "E", "O"],
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    right_before = _wkt_for(service, grid_id, (0, 3))
    response = service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "W"}])
    # (0,0) now leaves the rectangle, so outfall (0,1) loses its only cell;
    # outfall (0,3) is unrelated and untouched.
    assert response["catchment_transition"] == {
        "regenerated": [],
        "unchanged": [],
        "removed": [{"row": 0, "col": 1}],
    }
    assert _wkt_for(service, grid_id, (0, 3)) == right_before


def test_outfall_that_loses_its_last_cell_is_removed_from_export(service):
    # Outfall (0,1) is fed only by (0,0).
    grid_id = service.import_grid([["E", "O"], ["O", "O"]])["grid_id"]
    assert _exported_outfalls(service, grid_id) == {(0, 1)}

    # Send the only supplier out of bounds instead.
    response = service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "N"}])
    assert response["catchment_transition"]["removed"] == [{"row": 0, "col": 1}]
    assert (0, 1) not in _exported_outfalls(service, grid_id)
    assert service.catchment_export(grid_id) == []
    # The CSV is still a valid header-only document.
    lines = service.catchment_csv(grid_id).strip().splitlines()
    assert lines == ["grid_id,outfall_row,outfall_col,cell_count,wkt"]


def test_regaining_cells_recreates_a_previously_removed_catchment(service):
    grid_id = service.import_grid([["E", "O"], ["O", "O"]])["grid_id"]
    service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "N"}])
    assert service.catchment_export(grid_id) == []
    service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "E"}])
    rows = service.catchment_export(grid_id)
    assert len(rows) == 1
    assert (rows[0]["outfall_row"], rows[0]["outfall_col"]) == (0, 1)
    assert rows[0]["wkt"] == catchment_wkt({(0, 0)})


def test_catchment_failure_rolls_back_the_whole_resurvey(service, monkeypatch):
    grid_id = service.import_grid(
        [
            ["E", "E", "O"],
            ["O", "O", "O"],
        ]
    )["grid_id"]
    before_wkt = _wkt_for(service, grid_id, (0, 2))
    before_state = service.store.cell_state(grid_id, (0, 0))
    before_supply = service.supply(grid_id)

    def boom(_cells):
        raise RuntimeError("polygon engine down")

    monkeypatch.setattr("app.service.catchment_wkt", boom)
    with pytest.raises(RuntimeError, match="polygon engine down"):
        service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "S"}])

    # Nothing changed: directions, flow results and catchments all stay queryable.
    assert service.store.cell_state(grid_id, (0, 0)) == before_state
    assert service.supply(grid_id) == before_supply
    assert _wkt_for(service, grid_id, (0, 2)) == before_wkt


def test_persist_failure_rolls_back_catchments_too(service, monkeypatch):
    grid_id = service.import_grid([["E", "E", "O"]])["grid_id"]
    before = service.catchment_export(grid_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(service.store, "replace_catchments", boom)
    with pytest.raises(RuntimeError, match="disk on fire"):
        service.resurvey(grid_id, [{"row": 0, "col": 1, "value": "?"}])
    assert service.catchment_export(grid_id) == before
    # The direction change rolled back as well.
    assert service.store.cell_state(grid_id, (0, 1)) == ("direction", "E")


def test_legacy_grid_without_catchments_is_backfilled_on_first_export(service):
    # Simulate a grid imported before the catchment feature existed.
    grid = [["E", "E", "O"]]
    grid_id = service.import_grid(grid)["grid_id"]
    service.store.conn.execute("DELETE FROM catchments WHERE grid_id = ?", (grid_id,))
    assert service.store.catchment_rows(grid_id) == []

    rows = service.catchment_export(grid_id)
    assert len(rows) == 1
    assert rows[0]["wkt"] == catchment_wkt({(0, 0), (0, 1)})
    # The back-fill was persisted: a second export reads stored rows.
    assert len(service.store.catchment_rows(grid_id)) == 1


def test_backfill_failure_leaves_flow_data_intact(service, monkeypatch):
    grid_id = service.import_grid([["E", "E", "O"]])["grid_id"]
    service.store.conn.execute("DELETE FROM catchments WHERE grid_id = ?", (grid_id,))
    before_supply = service.supply(grid_id)

    def boom(*_args, **_kwargs):
        raise RuntimeError("write denied")

    monkeypatch.setattr(service.store, "replace_catchments", boom)
    with pytest.raises(RuntimeError, match="write denied"):
        service.catchment_export(grid_id)
    assert service.supply(grid_id) == before_supply


def test_catchment_cell_count_matches_supply_after_many_resurveys(service):
    # Deterministic churn: one outfall per row in the last column; every other
    # cell points east and is supplied at the start.
    rows, cols = 4, 5
    grid = [
        ["E" if c < cols - 1 else "O" for c in range(cols)]
        for _ in range(rows)
    ]
    grid_id = service.import_grid(grid)["grid_id"]
    initial = service._supplied_sets(grid_id)
    assert sum(len(cells) for cells in initial.values()) == rows * (cols - 1)

    sequence = [
        (0, 0, "S"), (1, 1, "?"), (2, 2, "N"), (3, 3, "W"),
        (0, 0, "E"), (1, 1, "E"), (2, 2, "E"), (3, 3, "E"),
        (0, 3, "S"), (2, 0, "NE"), (1, 2, "S"), (3, 1, "N"),
    ]
    saw_regen = saw_removal = False
    for row, col, value in sequence:
        response = service.resurvey(
            grid_id, [{"row": row, "col": col, "value": value}]
        )
        saw_regen = saw_regen or bool(response["catchment_transition"]["regenerated"])
        saw_removal = saw_removal or bool(response["catchment_transition"]["removed"])

        # Stored WKT must equal a from-scratch build from the current supply sets.
        live_sets = {
            outfall: cells
            for outfall, cells in service._supplied_sets(grid_id).items()
            if cells
        }
        expected = {outfall: catchment_wkt(cells) for outfall, cells in live_sets.items()}
        stored = {
            (r["outfall_row"], r["outfall_col"]): r["wkt"]
            for r in service.store.catchment_rows(grid_id)
        }
        assert stored == expected
        counts = {
            (r["outfall_row"], r["outfall_col"]): r["cell_count"]
            for r in service.store.catchment_rows(grid_id)
        }
        assert counts == {outfall: len(cells) for outfall, cells in live_sets.items()}

    assert saw_regen and saw_removal


def test_noop_resurvey_does_not_touch_catchments(service):
    grid_id = service.import_grid([["E", "E", "O"]])["grid_id"]
    before = service.store.catchment_rows(grid_id)
    response = service.resurvey(grid_id, [{"row": 0, "col": 0, "value": "E"}])
    assert response["catchment_transition"] == {
        "regenerated": [],
        "unchanged": [],
        "removed": [],
    }
    assert [dict(row) for row in service.store.catchment_rows(grid_id)] == [
        dict(row) for row in before
    ]
