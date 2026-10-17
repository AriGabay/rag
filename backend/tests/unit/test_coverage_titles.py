"""Which other documents a focused answer names as matching the question by title. Synthetic titles only."""

from app.chat.coverage import less_named


def test_a_title_carrying_fewer_of_the_questions_words_is_not_named_whatever_its_spelling():
    cited = {"c": "שומה מבני אחסנה אזור התעשיה הצפוני עין ורד"}
    others = {"o": "שומה רחוב הנפחים 4 אזור התעשייה כפר נוף"}
    question = "מה שיעור ההיוון בנכס באזור התעשייה הצפוני בעין ורד?"
    assert less_named(question, others, cited) == {}


def test_a_title_carrying_as_many_of_the_questions_words_stays_named():
    cited = {"c": "שומה הגפן 12 ברושים"}
    others = {"o": "שומה הזית 7 ברושים"}
    assert less_named("מה השווי בברושים?", others, cited) == others
