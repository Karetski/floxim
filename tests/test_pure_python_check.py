"""The CI check that every installed runtime distribution is pure Python (ADR 0001)."""

from check_pure_python import find_impure

PURE = "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def test_given_only_pure_wheels_when_checked_then_nothing_is_reported() -> None:
    # Given
    wheels = {
        "arcflow": PURE,
        "legacy-pure": "Root-Is-Purelib: true\nTag: py2-none-any\nTag: py3-none-any\n",
    }

    # When / Then
    assert find_impure(wheels) == []


def test_given_a_native_wheel_when_checked_then_it_is_reported() -> None:
    # Given
    wheels = {
        "arcflow": PURE,
        "rpds-py": "Root-Is-Purelib: false\nTag: cp312-cp312-macosx_11_0_arm64\n",
    }

    # When
    impure = find_impure(wheels)

    # Then
    assert [name for name, _ in impure] == ["rpds-py"]


def test_given_a_pure_tag_on_a_platlib_wheel_when_checked_then_it_is_reported() -> None:
    # Given
    wheels = {"odd": "Root-Is-Purelib: false\nTag: py3-none-any\n"}

    # When / Then
    assert [name for name, _ in find_impure(wheels)] == ["odd"]


def test_given_a_distribution_without_wheel_metadata_when_checked_then_it_is_reported() -> None:
    # Given
    wheels = {"from-egg": None}

    # When / Then
    assert [name for name, _ in find_impure(wheels)] == ["from-egg"]
