from cwatlas_dash import sources


def test_solar_priorities_shape():
    p = sources.solar_priorities(35.0, -97.0)   # anywhere; shape is what's tested
    assert isinstance(p["phase"], str) and p["phase"]
    assert p["weights"] and all(isinstance(w, float) for w in p["weights"].values())
    assert "20m" in p["weights"]
    assert p["nudges"] is None          # live nudges need MCP; explicit in UI
