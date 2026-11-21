"""The target reports the installed package's version (no database needed)."""

from importlib.metadata import version

from target_clickhouse.target import TargetClickhouse


def test_plugin_version_is_the_installed_package_version():
    """`--about` shows the distribution's version, not "could not be detected"."""
    assert TargetClickhouse.get_plugin_version() == version(
        "akurdyukov-target-clickhouse",
    )
