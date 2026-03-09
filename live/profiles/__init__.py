"""Checked-in runner profiles for deployment-style live processes."""
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import tomllib


class ProfileError(ValueError):
    """Invalid runner profile."""


_PACKAGE_DIR = Path(__file__).resolve().parent
_CATALOG_DIR = _PACKAGE_DIR / "catalog"
_ALLOWED_TOP_LEVEL_KEYS = {
    "strategy",
    "slug_pattern",
    "hours_ahead",
    "mode",
    "binance_feed",
    "run_secs",
    "description",
    "strategy_config",
}
_ALLOWED_MODES = {"sandbox", "live"}
_ALLOWED_BINANCE_FEEDS = {"global", "us"}


@dataclass(frozen=True)
class RunnerProfile:
    name: str
    strategy: str
    slug_pattern: str
    hours_ahead: int
    mode: str
    binance_feed: str
    run_secs: int | None = None
    description: str | None = None
    strategy_config: dict[str, object] = field(default_factory=dict)

    @property
    def sandbox(self) -> bool:
        return self.mode == "sandbox"

    @property
    def binance_us(self) -> bool:
        return self.binance_feed == "us"

    def with_run_secs(self, run_secs: int | None) -> "RunnerProfile":
        if run_secs is not None and run_secs <= 0:
            raise ProfileError("Profile run_secs override must be a positive integer")
        return replace(self, run_secs=run_secs)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def available_profile_names() -> list[str]:
    return sorted(path.stem for path in _CATALOG_DIR.glob("*.toml"))


def load_profile(profile_name_or_path: str) -> RunnerProfile:
    path = _resolve_profile_path(profile_name_or_path)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"Invalid TOML in {path}: {exc}") from exc

    return _parse_profile(name=path.stem, data=data)


def profile_catalog_dir() -> Path:
    return _CATALOG_DIR


def _resolve_profile_path(profile_name_or_path: str) -> Path:
    candidate = Path(profile_name_or_path)
    if candidate.exists():
        return candidate.resolve()

    profile_name = candidate.stem or profile_name_or_path
    path = _CATALOG_DIR / f"{profile_name}.toml"
    if path.exists():
        return path

    available = ", ".join(available_profile_names()) or "(none)"
    raise ProfileError(f"Unknown runner profile {profile_name_or_path!r}. Available: {available}")


def _parse_profile(*, name: str, data: dict[str, object]) -> RunnerProfile:
    if not isinstance(data, dict):
        raise ProfileError(f"Profile {name!r} must decode to a TOML table")

    unknown_keys = sorted(set(data) - _ALLOWED_TOP_LEVEL_KEYS)
    if unknown_keys:
        joined = ", ".join(unknown_keys)
        raise ProfileError(f"Profile {name!r} has unknown key(s): {joined}")

    strategy = _required_str(data, "strategy", name)
    slug_pattern = _required_str(data, "slug_pattern", name)
    mode = _required_str(data, "mode", name)
    binance_feed = _required_str(data, "binance_feed", name)
    hours_ahead = _required_positive_int(data, "hours_ahead", name)
    run_secs = _optional_positive_int(data, "run_secs", name)
    description = _optional_str(data, "description", name)
    strategy_config = data.get("strategy_config", {})

    if mode not in _ALLOWED_MODES:
        allowed = ", ".join(sorted(_ALLOWED_MODES))
        raise ProfileError(f"Profile {name!r} mode must be one of: {allowed}")
    if binance_feed not in _ALLOWED_BINANCE_FEEDS:
        allowed = ", ".join(sorted(_ALLOWED_BINANCE_FEEDS))
        raise ProfileError(f"Profile {name!r} binance_feed must be one of: {allowed}")
    if not isinstance(strategy_config, dict):
        raise ProfileError(f"Profile {name!r} strategy_config must be a TOML table")

    return RunnerProfile(
        name=name,
        strategy=strategy,
        slug_pattern=slug_pattern,
        hours_ahead=hours_ahead,
        mode=mode,
        binance_feed=binance_feed,
        run_secs=run_secs,
        description=description,
        strategy_config=dict(strategy_config),
    )


def _required_str(data: dict[str, object], key: str, name: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProfileError(f"Profile {name!r} requires non-empty string {key!r}")
    return value


def _optional_str(data: dict[str, object], key: str, name: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProfileError(f"Profile {name!r} {key!r} must be a non-empty string when set")
    return value


def _required_positive_int(data: dict[str, object], key: str, name: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ProfileError(f"Profile {name!r} {key!r} must be a positive integer")
    return value


def _optional_positive_int(data: dict[str, object], key: str, name: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or value <= 0:
        raise ProfileError(f"Profile {name!r} {key!r} must be a positive integer when set")
    return value
