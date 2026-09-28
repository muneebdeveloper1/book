from src.narration.voice_profiles import get_profile, infer_profile


def test_default_profile():
    assert get_profile().voice == 'am_adam'


def test_genre_profiles():
    assert infer_profile('a terrifying haunted house') == 'horror'
    assert infer_profile('how to sleep and relax') == 'relaxation'
    assert infer_profile('discipline and motivation') == 'motivation'
    assert infer_profile('history of ancient Rome') == 'documentary'
    assert infer_profile('एक हिंदी कहानी', 'Hindi') == 'hindi'
