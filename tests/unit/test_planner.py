from gateway.app.backend.planner import composition_count, plan_tc


def test_tc02_plans_lens_matrix():
    plan = plan_tc(
        "tc02",
        {"product": ["p1", "p2"], "background": ["bg"]},
        {"use_center": True, "use_left": False, "use_right": True},
    )
    assert plan["composition_count"] == 2
    assert plan["reframe_per_source"] == 14
    assert plan["final_count"] == 28


def test_tc03_uses_duration_assumption():
    plan = plan_tc(
        "tc03",
        {"product": ["p1"]},
        {"assume_duration_seconds": 25, "segment_duration": 10},
    )
    assert plan["segment_count_assumption"] == 3
    assert plan["final_count"] == 3


def test_tc04_counts_reframe_and_batch_stages():
    plan = plan_tc(
        "tc04",
        {"product": ["p1"]},
        {"assume_duration_seconds": 21, "segment_duration": 10},
    )
    assert plan["final_count"] == 63
    assert plan["planned_stage_count"] == 84


def test_tc05_uses_sources_not_products():
    plan = plan_tc("tc05", {"source": ["s1", "s2"]}, {})
    assert plan["final_count"] == 42
    assert plan["products"] == 0
    assert plan["sources"] == 2


def test_tc06_prefers_product_roots():
    plan = plan_tc("tc06", {"product_root": ["r1", "r2"]}, {})
    assert plan["final_count"] == 2
    assert composition_count({"compositions": ["center", "left"]}) == 2
