import re
import tomllib

PRESETS = ("release-only", "combined-lanes-release", "each-lane-release", "every-task-lane-release")


RESOURCE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")


class PolicyError(ValueError):
    pass


def require(condition, path, message):
    if not condition:
        raise PolicyError(f"{path}: {message}")


def table(value, path, required, optional=()):
    require(type(value) is dict, path, "expected a table")
    require(not (set(value) - set(required) - set(optional)), path, f"unknown fields: {sorted(set(value) - set(required) - set(optional))}")
    require(set(required) <= set(value), path, f"required fields: {', '.join(required)}")


def text(value, path):
    require(type(value) is str and bool(value.strip()) and not any(ord(c) < 32 or ord(c) == 127 for c in value), path, "expected a nonempty string without control characters")


def choice(value, path, choices):
    require(type(value) is str and value in choices, path, f"expected one of {', '.join(choices)}")


def strings(value, path, nonempty=True):
    require(type(value) is list and (bool(value) or not nonempty), path, "expected an array of strings" + (" with at least one entry" if nonempty else ""))
    for i, item in enumerate(value):
        text(item, f"{path}[{i}]")
    require(len(value) == len(set(value)), path, "duplicate entries are not allowed")


def validate(data):
    table(data, "policy", ("schema", "project", "staging", "release", "fully_verified_release", "authority", "gates", "checks", "limits"), ("assembly", "selection", "artifacts", "exclusive", "guest"))
    require(type(data["schema"]) is int and data["schema"] == 1, "schema", "only integer schema version 1 is supported")
    text(data["project"], "project")
    for key in ("staging", "release"):
        value = data[key]
        text(value, key)
        require(not value.startswith(("-", "/", ".")) and not value.endswith(("/", ".")) and not any(x in value for x in ("..", "@{", "//")) and not re.search(r"[\s~^:?*\[\\]", value) and all(not x.endswith(".lock") and not x.startswith(".") for x in value.split("/")) and value != "@", key, "expected a valid Git branch name, for example dev or main")
    require(data["staging"] != data["release"], "release", "must differ from staging")
    require(type(data["fully_verified_release"]) is bool, "fully_verified_release", "expected a boolean")
    table(data["authority"], "authority", ("mode",))
    choice(data["authority"]["mode"], "authority.mode", ("local-bare",))
    if "assembly" in data:
        table(data["assembly"], "assembly", (), ("excluded",))
        if "excluded" in data["assembly"]:
            strings(data["assembly"]["excluded"], "assembly.excluded", False)
            for i, pattern in enumerate(data["assembly"]["excluded"]):
                require(not pattern.startswith("/") and pattern == pattern.strip(), f"assembly.excluded[{i}]", "expected a relative path glob without surrounding whitespace")
    checks = data["checks"]
    require(type(checks) is dict and bool(checks), "checks", "expected a nonempty table of check IDs to argv arrays")
    for name, argv in checks.items():
        require(bool(re.fullmatch(r"[a-z][a-z0-9_-]*", name)), "checks", f"invalid check ID {name!r}")
        require(type(argv) is list and bool(argv), f"checks.{name}", "expected a nonempty argv array, not a shell command")
        for i, arg in enumerate(argv):
            require(type(arg) is str and "\x00" not in arg and (i != 0 or bool(arg.strip())), f"checks.{name}[{i}]", "expected a string without NUL; executable must be nonempty")
    table(data["gates"], "gates", ("task", "staging", "release"))
    for name, gate in data["gates"].items():
        path = f"gates.{name}"
        table(gate, path, ("scope",), ("executor", "environment", "checks", "approvals", "mode") if name == "staging" else ("executor", "environment", "checks", "approvals"))
        choice(gate["scope"], path + ".scope", ("none", "affected", "full"))
        if "approvals" in gate:
            require(type(gate["approvals"]) is int and 0 <= gate["approvals"] <= 16, path + ".approvals", "expected an integer between 0 and 16, not a boolean")
        if name == "staging":
            choice(gate.get("mode"), path + ".mode", ("each-lane", "combined-ready"))
        for key, choices in (("executor", ("local", "github")), ("environment", ("native", "guest"))):
            if key in gate:
                choice(gate[key], path + "." + key, choices)
            elif gate["scope"] != "none":
                raise PolicyError(f"{path}.{key}: required for an enabled gate")
        if "checks" in gate:
            strings(gate["checks"], path + ".checks", gate["scope"] != "none")
            require(set(gate["checks"]) <= set(checks), path + ".checks", "every check ID must exist in [checks]")
        else:
            require(gate["scope"] == "none", path + ".checks", "an enabled gate needs at least one check")
    require(not data["fully_verified_release"] or data["gates"]["release"]["scope"] == "full", "gates.release.scope", "a fully verified release requires full coverage")
    limits = {"cpu_slots": (1, 1024), "memory_mib": (1, 2**30), "disk_mib": (1, 2**40), "max_guests": (0, 1024), "timeout_seconds": (1, 604800)}
    table(data["limits"], "limits", tuple(limits))
    for name, (low, high) in limits.items():
        value = data["limits"][name]
        require(type(value) is int and low <= value <= high, f"limits.{name}", f"expected an integer between {low} and {high}, not a boolean")
    for name, gate in data["gates"].items():
        require(not (gate["scope"] != "none" and gate.get("environment") == "guest" and data["limits"]["max_guests"] == 0), f"gates.{name}.environment", "guest gates require limits.max_guests >= 1")
    for section in ("selection", "artifacts", "exclusive"):
        if section not in data:
            continue
        require(type(data[section]) is dict, section, "expected a table of check IDs to arrays")
        for name, entries in data[section].items():
            path = f"{section}.{name}"
            require(name in checks, section, f"unknown check ID {name!r}")
            strings(entries, path)
            for i, entry in enumerate(entries):
                if section == "exclusive":
                    require(bool(RESOURCE.fullmatch(entry)), f"{path}[{i}]", "expected a resource name of lowercase letters, digits, dots, hyphens or underscores, for example port-5432")
                elif section == "artifacts":
                    require(relative(entry) and not any(c in entry for c in "*?[\\"), f"{path}[{i}]", "expected a normalized relative file path without glob characters")
                else:
                    require(not entry.startswith("/") and entry == entry.strip(), f"{path}[{i}]", "expected a relative path glob without surrounding whitespace")
    if "guest" in data:
        table(data["guest"], "guest", ("image",))
        require(type(data["guest"]["image"]) is str and bool(IMAGE.fullmatch(data["guest"]["image"])), "guest.image", "expected an image pinned by digest, name@sha256:<64 lowercase hex>")
    require("guest" in data or not any(gate["scope"] != "none" and gate.get("environment") == "guest" for gate in data["gates"].values()), "guest.image", "required when an enabled gate uses environment guest")
    return data


def relative(path):
    return not path.startswith("/") and all(part not in ("", ".", "..") for part in path.split("/"))


def parse(source):
    try:
        return validate(tomllib.loads(source))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"TOML: {exc}") from exc
    except RecursionError as exc:
        raise PolicyError("TOML: nesting exceeds the supported parser depth") from exc


def preset(name, executor="local"):
    choice(name, "preset", PRESETS)
    choice(executor, "executor", ("local", "github"))
    gates = {}
    for gate in ("task", "staging", "release"):
        enabled = gate == "release" or (gate == "staging" and name != PRESETS[0]) or (gate == "task" and name == PRESETS[3])
        gates[gate] = {"scope": "full" if enabled else "none", "executor": executor, "environment": "native", "checks": ["example"] if enabled else []}
    gates["staging"]["mode"] = "combined-ready" if name == PRESETS[1] else "each-lane"
    return validate({"schema": 1, "project": "example-project", "staging": "dev", "release": "main", "fully_verified_release": True, "authority": {"mode": "local-bare"}, "gates": gates, "checks": {"example": ["python3", "-c", "raise SystemExit('Replace this synthetic example with reviewed project checks')"]}, "limits": {"cpu_slots": 2, "memory_mib": 4096, "disk_mib": 10240, "max_guests": 0, "timeout_seconds": 1800}})


def render(data):
    import json
    validate(data)
    def value(item):
        return json.dumps(item, ensure_ascii=False).replace("\x7f", "\\u007f")
    lines = [f"{key} = {value(data[key])}" for key in ("schema", "project", "staging", "release", "fully_verified_release")]
    for section, entries in [("authority", data["authority"]), *((("assembly", data["assembly"]),) if "assembly" in data else ()), *((f"gates.{name}", gate) for name, gate in data["gates"].items()), ("checks", data["checks"]), *((section, data[section]) for section in ("selection", "artifacts", "exclusive", "guest") if section in data), ("limits", data["limits"])]:
        lines.extend(["", f"[{section}]"])
        lines.extend(f"{key} = {value(item)}" for key, item in entries.items())
    return "\n".join(lines) + "\n"
