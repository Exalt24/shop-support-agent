from agent.policy import retrieve


def test_a_returns_question_lands_on_the_returns_section(policy):
    assert retrieve("Can I return something I bought?", policy)[0].heading == "Returns"


def test_a_warranty_question_lands_on_warranty(policy):
    assert retrieve("how long is the warranty on electronics", policy)[0].heading == "Warranty"


def test_gibberish_matches_nothing(policy):
    assert retrieve("zzz qqq xxx", policy) == []
