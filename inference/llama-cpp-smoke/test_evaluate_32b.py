from evaluate_32b import QUALITY_CASES, context_within_limit, long_fact_check, make_long_prompt, objective_check


def test_reference_answers_match_quality_rubrics():
    answers = [
        '{"project":"Cedar-17","owner":"Leena Rao","deadline":"2026-11-04 16:30 UTC"}',
        "Larch-3 at 15:20: 48 services passed, rollback is available Friday, and customer data was unchanged.",
        "Subtotal $5,040.00; total $5,443.20.",
        "A: blue\nB: 14\nC: no",
        "The function returns ['Mina', 'Omar'] because it includes scores of 70 or above.",
        "Cobalt-2 ran 71 minutes, reached 63 degrees Celsius, and had no leak.",
    ]
    for case, answer in zip(QUALITY_CASES, answers):
        assert objective_check(case, answer)["passed"], case["id"]


def test_code_rubric_requires_exact_returned_names_and_threshold():
    case = next(case for case in QUALITY_CASES if case["id"] == "code-explanation")
    answer = "It returns ['Mina', 'Nia'] because scores are at least 70."
    assert not objective_check(case, answer)["passed"]


def test_long_prompt_places_facts_in_order():
    prompt = make_long_prompt(8)
    assert prompt.index("ORBIT-741") < prompt.index("mica-3") < prompt.index("Imani")
    assert long_fact_check("ORBIT-741 at R-12, mica-3 checksum 83A9; Imani owns the six-minute rollback.")["passed"]
    assert not long_fact_check("ORBIT-741 at R-12 only.")["passed"]


def test_shared_context_budget_accounts_for_each_request_and_reserve():
    responses = [
        {"ok": True, "tokens_evaluated": 6112, "truncated": False},
        {"ok": True, "tokens_evaluated": 6112, "truncated": False},
    ]
    assert context_within_limit(responses, 64, 16384)
    assert not context_within_limit(responses, 64, 12000)
    responses[1]["truncated"] = True
    assert not context_within_limit(responses, 64, 16384)