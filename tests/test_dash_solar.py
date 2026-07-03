from cwatlas_dash import sources


def test_solar_priorities_shape():
    p = sources.solar_priorities(33.427, -82.208)
    assert isinstance(p["phase"], str) and p["phase"]
    assert p["weights"] and all(isinstance(w, float) for w in p["weights"].values())
    assert "20m" in p["weights"]
    assert p["nudges"] is None          # live nudges need MCP; explicit in UI
