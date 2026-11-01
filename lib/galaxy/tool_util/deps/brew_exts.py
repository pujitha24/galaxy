#!/usr/bin/env python

# % brew vinstall samtools 1.0
# % brew vinstall samtools 0.1.19
# % brew vinstall samtools 1.1
# % brew env samtools 1.1
# PATH=/home/john/.linuxbrew/Cellar/htslib/1.1/bin:/home/john/.linuxbrew/Cellar/samtools/1.1/bin:$PATH
# export PATH
# LD_LIBRARY_PATH=/home/john/.linuxbrew/Cellar/htslib/1.1/lib:/home/john/.linuxbrew/Cellar/samtools/1.1/lib:$LD_LIBRARY_PATH
# export LD_LIBRARY_PATH
# % . <(brew env samtools 1.1)
# % which samtools
# /home/john/.linuxbrew/Cellar/samtools/1.1/bin/samtools
# % . <(brew env samtools 0.1.19)
# % which samtools
# /home/john/.linuxbrew/Cellar/samtools/0.1.19/bin/samtools
# % brew vuninstall samtools 1.0
# % brew vdeps samtools 1.1
# htslib@1.1
# % brew vdeps samtools 0.1.19


import argparse
import contextlib
import glob
import json
import os
import re
import string
import subprocess
import sys
from collections.abc import Iterator
from shutil import which
from typing import (
    Any,
    TypedDict,
)

WHITESPACE_PATTERN = re.compile(r"[\s]+")

DESCRIPTION = "Script built on top of linuxbrew to operate on isolated, versioned brew installed environments."

if sys.platform == "darwin":
    DEFAULT_HOMEBREW_ROOT = "/usr/local"
else:
    DEFAULT_HOMEBREW_ROOT = os.path.join(os.path.expanduser("~"), ".linuxbrew")

NO_BREW_ERROR_MESSAGE = "Could not find brew on PATH, please place on path or pass to script with --brew argument."
CANNOT_DETERMINE_TAP_ERROR_MESSAGE = (
    "Cannot determine tap of specified recipe - please use fully qualified recipe (e.g. homebrew/science/samtools)."
)
VERBOSE = False
RELAXED = False
BREW_ARGS: list[str] = []


class VersionedDep(TypedDict):
    """A dependency entry in the ``INSTALL_RECEIPT_VERSIONED.json`` file."""

    name: str
    version: str | None
    versioned: bool


class BrewContext:
    def __init__(self, args: argparse.Namespace | None = None) -> None:
        ensure_brew_on_path(args)
        raw_config = brew_execute(["config"])
        config_lines = [line.strip().split(":", 1) for line in raw_config.split("\n") if line]
        config = {p[0].strip(): p[1].strip() for p in config_lines}
        # unset if "/usr/local" -> https://github.com/Homebrew/homebrew/blob/master/Library/Homebrew/cmd/config.rb
        homebrew_prefix = config.get("HOMEBREW_PREFIX", "/usr/local")
        homebrew_cellar = config.get("HOMEBREW_CELLAR", os.path.join(homebrew_prefix, "Cellar"))
        self.homebrew_prefix = homebrew_prefix
        self.homebrew_cellar = homebrew_cellar


class RecipeContext:
    @staticmethod
    def from_args(args: argparse.Namespace, brew_context: BrewContext | None = None) -> "RecipeContext":
        return RecipeContext(args.recipe, args.version, brew_context)

    def __init__(self, recipe: str, version: str, brew_context: BrewContext | None = None) -> None:
        self.recipe = recipe
        self.version = version
        self.brew_context = brew_context or BrewContext()

    @property
    def cellar_path(self) -> str:
        return recipe_cellar_path(self.brew_context.homebrew_cellar, self.recipe, self.version)

    @property
    def tap_path(self) -> str:
        return os.path.join(self.brew_context.homebrew_prefix, "Library", "Taps", self.__tap_path(self.recipe))

    def __tap_path(self, recipe: str) -> str:
        parts = recipe.split("/")
        if len(parts) == 1:
            info = brew_info(self.recipe)
            from_url: str | None = info["from_url"]
            if not from_url:
                raise Exception(CANNOT_DETERMINE_TAP_ERROR_MESSAGE)
            from_url_parts = from_url.split("/")
            blob_index = from_url_parts.index("blob")  # comes right after username and repository
            if blob_index < 2:
                raise Exception(CANNOT_DETERMINE_TAP_ERROR_MESSAGE)
            username = from_url_parts[blob_index - 2]
            repository = from_url_parts[blob_index - 1]
        else:
            assert len(parts) == 3
            parts = recipe.split("/")
            username = parts[0]
            repository = f"homebrew-{parts[1]}"

        path = os.path.join(username, repository)
        return path


def main() -> None:
    global VERBOSE
    global RELAXED
    global BREW_ARGS
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--brew", help="Path to linuxbrew 'brew' executable to target")
    actions = ["vinstall", "vuninstall", "vdeps", "vinfo", "env"]
    action = __action(sys.argv)
    if not action:
        parser.add_argument("action", metavar="action", help="Versioned action to perform.", choices=actions)
    parser.add_argument(
        "recipe", metavar="recipe", help="Recipe for action - should be absolute (e.g. homebrew/science/samtools)."
    )
    parser.add_argument("version", metavar="version", help="Version for action (e.g. 0.1.19).")
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Relaxed processing - for instance allow use of env on non-vinstall-ed recipes.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("restargs", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.verbose:
        VERBOSE = True
    if args.relaxed:
        RELAXED = True
    BREW_ARGS = args.restargs
    if not action:
        action = args.action
    brew_context = BrewContext(args)
    recipe_context = RecipeContext.from_args(args, brew_context)
    if action == "vinstall":
        versioned_install(recipe_context, args.recipe, args.version)
    elif action == "vuninstall":
        brew_execute(["switch", args.recipe, args.version])
        brew_execute(["uninstall", args.recipe])
    elif action == "vdeps":
        print_versioned_deps(recipe_context)
    elif action == "env":
        env_statements = build_env_statements_from_recipe_context(recipe_context)
        print(env_statements)
    elif action == "vinfo":
        with brew_head_at_version(recipe_context, args.recipe, args.version):
            print(brew_info(args.recipe))
    else:
        raise NotImplementedError()


class CommandLineException(Exception):
    def __init__(self, command: str, stdout: str, stderr: str) -> None:
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.message = (
            f"Failed to execute command-line {command}, stderr was:\n"
            "-------->>begin stderr<<--------\n"
            f"{stderr}\n"
            "-------->>end stderr<<--------\n"
            "-------->>begin stdout<<--------\n"
            f"{stdout}\n"
            "-------->>end stdout<<--------\n"
        )

    def __str__(self) -> str:
        return self.message


def versioned_install(
    recipe_context: RecipeContext,
    package: str,
    version: str,
    installed_deps: list[str] | None = None,
) -> None:
    if installed_deps is None:
        installed_deps = []

    attempt_unlink(package)
    with brew_head_at_version(recipe_context, package, version):
        deps = brew_deps(package)
        deps_metadata: list[VersionedDep] = []
        dep_to_version: dict[str, str | None] = {}
        for dep in deps:
            version_info = brew_versions_info(dep, recipe_context.tap_path)[0]
            versioned = version_info[2]
            if versioned:
                version_from_brew = version_info[0]
                dep_to_version[dep] = version_from_brew
                if dep in installed_deps:
                    continue
                versioned_install(recipe_context, dep, version_from_brew, installed_deps)
                installed_deps.append(dep)
            else:
                # Install latest.
                dep_to_version[dep] = None
                if dep in installed_deps:
                    continue
                unversioned_install(dep)
        try:
            for dep in deps:
                dep_version = dep_to_version[dep]
                if dep_version:
                    brew_execute(["switch", dep, dep_version])
                else:
                    brew_execute(["link", dep])
                # dep_version obtained from brew versions doesn't
                # include revision. This linked_keg attribute does.
                keg_version = brew_info(dep)["linked_keg"]
                dep_metadata = VersionedDep(name=dep, version=keg_version, versioned=bool(dep_version))
                deps_metadata.append(dep_metadata)

            cellar_root = recipe_context.brew_context.homebrew_cellar
            cellar_path = recipe_context.cellar_path
            env_actions = build_env_actions(deps_metadata, cellar_root, cellar_path, custom_only=True)
            env = EnvAction.build_env(env_actions)
            args = ["install"]
            if VERBOSE:
                args.append("--verbose")
            args.extend(BREW_ARGS)
            args.append(package)
            brew_execute(args, env=env)
            deps_str = brew_execute(["deps", package])
            deps = [d.strip() for d in deps_str.split("\n") if d]
            metadata = {"deps": deps_metadata}
            cellar_root = recipe_context.brew_context.homebrew_cellar
            cellar_path = recipe_cellar_path(cellar_root, package, version)
            v_metadata_path = os.path.join(cellar_path, "INSTALL_RECEIPT_VERSIONED.json")
            with open(v_metadata_path, "w") as f:
                json.dump(metadata, f)

        finally:
            attempt_unlink_all(package, deps)


def commit_for_version(recipe_context: RecipeContext, package: str, version: str | None) -> str:
    tap_path = recipe_context.tap_path
    commit = None
    with brew_head_at_commit("master", tap_path):
        version_to_commit = brew_versions_info(package, tap_path)
        if version is None:
            version = version_to_commit[0][0]
            commit = version_to_commit[0][1]
        else:
            for mapping in version_to_commit:
                if mapping[0] == version:
                    commit = mapping[1]
    if commit is None:
        raise Exception(f"Failed to find commit for version {version}")
    return commit


def print_versioned_deps(recipe_context: RecipeContext) -> None:
    deps = load_versioned_deps(recipe_context.cellar_path)
    for dep in deps:
        val = dep["name"]
        if dep["versioned"]:
            val += f"@{dep['version']}"
        print(val)


def load_versioned_deps(cellar_path: str, relaxed: bool | None = None) -> list[VersionedDep]:
    if relaxed is None:
        relaxed = RELAXED
    v_metadata_path = os.path.join(cellar_path, "INSTALL_RECEIPT_VERSIONED.json")
    if not os.path.isfile(v_metadata_path):
        if relaxed:
            return []
        else:
            raise OSError(f"Could not locate versioned receipt file: {v_metadata_path}")
    with open(v_metadata_path) as f:
        metadata = json.load(f)
    deps: list[VersionedDep] = metadata["deps"]
    return deps


def unversioned_install(package: str) -> None:
    try:
        deps = brew_deps(package)
        for dep in deps:
            brew_execute(["link", dep])
        brew_execute(["install", package])
    finally:
        attempt_unlink_all(package, deps)


def attempt_unlink_all(package: str, deps: list[str]) -> None:
    for dep in deps:
        attempt_unlink(dep)
    attempt_unlink(package)


def attempt_unlink(package: str) -> None:
    try:
        brew_execute(["unlink", package])
    except Exception:
        # TODO: warn
        pass


def brew_execute(args: list[str], env: dict[str, str] | None = None) -> str:
    os.environ["HOMEBREW_NO_EMOJI"] = "1"  # simplify brew parsing.
    cmds = ["brew"] + args
    return execute(cmds, env=env)


def build_env_statements_from_recipe_context(recipe_context: RecipeContext) -> str:
    cellar_root = recipe_context.brew_context.homebrew_cellar
    env_statements = build_env_statements(cellar_root, recipe_context.cellar_path)
    return env_statements


def build_env_statements(
    cellar_root: str, cellar_path: str, relaxed: bool | None = None, custom_only: bool = False
) -> str:
    deps = load_versioned_deps(cellar_path, relaxed=relaxed)
    actions = build_env_actions(deps, cellar_root, cellar_path, relaxed, custom_only)
    env_statements = []
    for action in actions:
        env_statements.extend(action.to_statements())
    return "\n".join(env_statements)


def build_env_actions(
    deps: list[VersionedDep],
    cellar_root: str,
    cellar_path: str,
    relaxed: bool | None = None,
    custom_only: bool = False,
) -> list["EnvAction"]:
    path_appends = []
    ld_path_appends = []
    actions: list[EnvAction] = []

    def handle_keg(cellar_path: str) -> None:
        bin_path = os.path.join(cellar_path, "bin")
        if os.path.isdir(bin_path):
            path_appends.append(bin_path)
        lib_path = os.path.join(cellar_path, "lib")
        if os.path.isdir(lib_path):
            ld_path_appends.append(lib_path)
        env_path = os.path.join(cellar_path, "platform_environment.json")
        if os.path.exists(env_path):
            with open(env_path) as f:
                env_metadata = json.load(f)
                if "actions" in env_metadata:

                    def to_action(desc: dict[str, str]) -> EnvAction:
                        return EnvAction(cellar_path, desc)

                    actions.extend(map(to_action, env_metadata["actions"]))

    for dep in deps:
        package = dep["name"]
        version = dep["version"]
        if version is None:
            continue
        dep_cellar_path = recipe_cellar_path(cellar_root, package, version)
        handle_keg(dep_cellar_path)

    handle_keg(cellar_path)
    if not custom_only:
        if path_appends:
            actions.append(
                EnvAction(cellar_path, {"action": "prepend", "variable": "PATH", "value": ":".join(path_appends)})
            )
        if ld_path_appends:
            actions.append(
                EnvAction(
                    cellar_path,
                    {"action": "prepend", "variable": "LD_LIBRARY_PATH", "value": ":".join(ld_path_appends)},
                )
            )
    return actions


class EnvAction:
    def __init__(self, keg_root: str, action_description: dict[str, str]) -> None:
        self.variable = action_description["variable"]
        self.action = action_description["action"]
        self.value = string.Template(action_description["value"]).safe_substitute(
            {
                "KEG_ROOT": keg_root,
            }
        )

    @staticmethod
    def build_env(env_actions: list["EnvAction"]) -> dict[str, str]:
        new_env = os.environ.copy()
        for env_action in env_actions:
            env_action.modify_environ(new_env)
        return new_env

    def modify_environ(self, environ: dict[str, str]) -> None:
        if self.action == "set" or not environ.get(self.variable, ""):
            environ[self.variable] = self.__eval("${value}")
        elif self.action == "prepend":
            environ[self.variable] = self.__eval(f"${{value}}:{environ[self.variable]}")
        else:
            environ[self.variable] = self.__eval(f"{environ[self.variable]}:${{value}}")

    def __eval(self, template: str) -> str:
        return string.Template(template).safe_substitute(
            variable=self.variable,
            value=self.value,
        )

    def to_statements(self) -> list[str]:
        if self.action == "set":
            template = '''${variable}="${value}"'''
        elif self.action == "prepend":
            template = '''${variable}="${value}:$$${variable}"'''
        else:
            template = '''${variable}="$$${variable}:${value}"'''
        return [self.__eval(template), f"export {self.variable}"]


@contextlib.contextmanager
def brew_head_at_version(recipe_context: RecipeContext, package: str, version: str | None) -> Iterator[None]:
    commit = commit_for_version(recipe_context, package, version)
    tap_path = recipe_context.tap_path
    with brew_head_at_commit(commit, tap_path):
        yield


@contextlib.contextmanager
def brew_head_at_commit(commit: str, tap_path: str) -> Iterator[None]:
    try:
        os.chdir(tap_path)
        current_commit = git_execute(["rev-parse", "HEAD"]).strip()
        try:
            git_execute(["checkout", commit])
            yield
        finally:
            git_execute(["checkout", current_commit])
    finally:
        # TODO: restore chdir - or better yet just don't chdir
        # shouldn't be needed.
        pass


def git_execute(args: list[str]) -> str:
    cmds = ["git"] + args
    return execute(cmds)


def execute(cmds: list[str], env: dict[str, str] | None = None) -> str:
    p = subprocess.Popen(cmds, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env or None, text=True)
    # log = p.stdout.read()
    stdout, stderr = p.communicate()
    if p.returncode != 0:
        raise CommandLineException(" ".join(cmds), stdout, stderr)
    if VERBOSE:
        print(stdout)
    return stdout


def brew_deps(package: str) -> list[str]:
    args = ["deps"]
    args.extend(BREW_ARGS)
    args.append(package)
    stdout = brew_execute(args)
    return [p.strip() for p in stdout.split("\n") if p]


def brew_info(recipe: str) -> dict[str, Any]:
    info_json = brew_execute(["info", "--json=v1", recipe])
    info = json.loads(info_json)[0]
    assert isinstance(info, dict)
    info.update(extended_brew_info(recipe))
    return info


def extended_brew_info(recipe: str) -> dict[str, Any]:
    # Extract more info from non-json variant. JSON variant should
    # include this in a backward compatible way (TODO: Open PR).
    raw_info = brew_execute(["info", recipe])
    extra_info: dict[str, Any] = dict(
        from_url=None,
        build_dependencies=[],
        required_dependencies=[],
        recommended_dependencies=[],
        optional_dependencies=[],
    )

    for line in raw_info.split("\n"):
        if line.startswith("From: "):
            extra_info["from_url"] = line[len("From: ") :].strip()
        for dep_type in ["Build", "Required", "Recommended", "Optional"]:
            if line.startswith(f"{dep_type}: "):
                key = f"{dep_type.lower()}_dependencies"
                raw_val = line[len(f"{dep_type}: ") :]
                extra_info[key].extend(raw_val.split(", "))
    return extra_info


def brew_versions_info(package: str, tap_path: str) -> list[tuple[str, str, bool]]:
    def versioned(recipe_path: str) -> bool:
        if not os.path.isabs(recipe_path):
            recipe_path = os.path.join(os.getcwd(), recipe_path)
        # Dependencies in the same repository should be versioned,
        # core dependencies (presumably in base homebrew) are not
        # versioned.
        return tap_path in recipe_path

    # TODO: Also use tags.
    stdout = brew_execute(["versions", package])
    stdout_lines = [line for line in stdout.split("\n") if line and "git checkout" in line]
    version_parts = [WHITESPACE_PATTERN.split(line) for line in stdout_lines]
    info = [(p[0], p[3], versioned(p[4])) for p in version_parts]
    return info


def __action(argv: list[str]) -> str | None:
    script_name = os.path.basename(argv[0])
    if script_name.startswith("brew-"):
        return script_name[len("brew-") :]
    else:
        return None


def recipe_cellar_path(cellar_path: str, recipe: str, version: str) -> str:
    recipe_base = recipe.split("/")[-1]
    recipe_base_path = os.path.join(cellar_path, recipe_base, version)
    if revision_paths := glob.glob(f"{recipe_base_path}_*"):
        revisions = (int(x.rsplit("_", 1)[-1]) for x in revision_paths)
        max_revision = max(revisions)
        recipe_path = f"{recipe_base_path}_{max_revision}"
    else:
        recipe_path = recipe_base_path
    return recipe_path


def ensure_brew_on_path(args: argparse.Namespace | None) -> None:
    brew_on_path = which("brew")
    if brew_on_path:
        brew_on_path = os.path.abspath(brew_on_path)

    def ensure_on_path(brew: str) -> None:
        if brew != brew_on_path:
            os.environ["PATH"] = f"{os.path.dirname(brew)}:{os.environ['PATH']}"

    default_brew_path = os.path.join(DEFAULT_HOMEBREW_ROOT, "bin", "brew")
    if args and args.brew:
        user_brew_path = os.path.abspath(args.brew)
        ensure_on_path(user_brew_path)
    elif brew_on_path:
        return
    elif os.path.exists(default_brew_path):
        ensure_on_path(default_brew_path)
    else:
        raise Exception(NO_BREW_ERROR_MESSAGE)


if __name__ == "__main__":
    main()
