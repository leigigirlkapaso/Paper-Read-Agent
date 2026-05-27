from modules.ideator.effort import EFFORT_PARAMS


def test_effort_params_has_all_levels():
    for level in ["lite", "balanced", "max", "beast"]:
        assert level in EFFORT_PARAMS


def test_effort_params_lite_keys_are_subset_of_upper_levels():
    lite_keys = set(EFFORT_PARAMS["lite"].keys())
    for level in ["balanced", "max", "beast"]:
        assert lite_keys.issubset(set(EFFORT_PARAMS[level].keys())), (
            f"{level} missing keys from lite: {lite_keys - set(EFFORT_PARAMS[level].keys())}"
        )
