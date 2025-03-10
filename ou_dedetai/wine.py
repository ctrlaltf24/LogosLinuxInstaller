from dataclasses import dataclass
import logging
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
from typing import Optional

from ou_dedetai import constants
from ou_dedetai.app import App

from . import network
from . import system
from . import utils

def check_wineserver(app: App):
    # FIXME: if the wine version changes, we may need to restart the wineserver
    # (or at least kill it). Gotten into several states in dev where this happend
    # Normally when an msi install failed
    try:
        process = run_wine_proc(app.conf.wineserver_binary, app)
        if not process:
            logging.debug("Failed to spawn wineserver to check it")
            return False
        process.wait()
        return process.returncode == 0
    except Exception:
        return False


def wineserver_kill(app: App):
    if check_wineserver(app):
        process = run_wine_proc(app.conf.wineserver_binary, app, exe_args=["-k"])
        if not process:
            logging.debug("Failed to spawn wineserver to kill it")
            return False
        process.wait()


def wineserver_wait(app: App):
    if check_wineserver(app):
        process = run_wine_proc(app.conf.wineserver_binary, app, exe_args=["-w"])
        if not process:
            logging.debug("Failed to spawn wineserver to wait for it")
            return False
        process.wait()


@dataclass
class WineRelease:
    major: int
    minor: int
    release: Optional[str]


def get_devel_or_stable(version: str) -> str:
    # Wine versioning states that x.0 is always stable branch, while x.y is devel.
    # Ref: https://gitlab.winehq.org/wine/wine/-/wikis/Wine-User's-Guide#wine-from-winehq
    if version.split('.')[1].startswith('0'):
        return 'stable'
    else:
        return 'devel'


# FIXME: consider raising exceptions on error
def get_wine_release(binary: str) -> tuple[Optional[WineRelease], str]:
    cmd = [binary, "--version"]
    try:
        version_string = subprocess.check_output(cmd, encoding='utf-8').strip()
        logging.debug(f"Version string: {str(version_string)}")
        branch: Optional[str]
        try:
            wine_version, branch = version_string.split()  # release = (Staging)
            branch = branch.lstrip('(').rstrip(')').lower()  # remove parens
        except ValueError:
            # Neither "Devel" nor "Stable" release is noted in version output
            wine_version = version_string
            branch = get_devel_or_stable(wine_version)
        version = wine_version.lstrip('wine-')
        logging.debug(f"Wine branch of {binary}: {branch}")

        ver_major = int(version.split('.')[0].lstrip('wine-'))  # remove 'wine-'
        ver_minor_str = version.split('.')[1]
        # In the case the version is an rc like wine-10.0-rc5
        if '-' in ver_minor_str:
            ver_minor_str = ver_minor_str.split("-")[0]
        ver_minor = int(ver_minor_str)

        wine_release = WineRelease(ver_major, ver_minor, branch)
        logging.debug(f"Wine release of {binary}: {str(wine_release)}")
        if ver_major == 0:
            return None, "Couldn't determine wine version."
        else:
            return wine_release, "yes"

    except subprocess.CalledProcessError as e:
        return None, f"Error running command: {e}"

    except ValueError as e:
        return None, f"Error parsing version: {e}"

    except Exception as e:
        return None, f"Error: {e}"


@dataclass
class WineRule:
    major: int
    proton: bool
    minor_bad: list[int]
    allowed_releases: list[str]
    devel_allowed: Optional[int] = None


def check_wine_rules(
    wine_release: Optional[WineRelease],
    release_version: Optional[str],
    faithlife_product_version: str
):
    # Does not check for Staging. Will not implement: expecting merging of
    # commits in time.
    logging.debug(f"Checking {wine_release} for {release_version}.")
    if faithlife_product_version == "10":
        if release_version is not None and utils.check_logos_release_version(release_version, 30, 1): #noqa: E501
            required_wine_minimum = [7, 18]
        else:
            required_wine_minimum = [9, 10]
    elif faithlife_product_version == "9":
        required_wine_minimum = [7, 0]
    else:
        raise ValueError(f"Invalid target version, expecting 9 or 10 but got: {faithlife_product_version} ({type(faithlife_product_version)})")  # noqa: E501

    rules: list[WineRule] = [
        # Proton release tend to use the x.0 release, but can include changes found in devel/staging  # noqa: E501
        # exceptions to minimum
        WineRule(major=7, proton=True, minor_bad=[], allowed_releases=["staging"]),
        # devel permissible at this point
        WineRule(major=8, proton=False, minor_bad=[0], allowed_releases=["staging"], devel_allowed=16), #noqa: E501
        WineRule(major=9, proton=False, minor_bad=[], allowed_releases=["devel", "staging"]),  #noqa: E501
        WineRule(major=10, proton=False, minor_bad=[], allowed_releases=["stable", "devel", "staging"]) #noqa: E501
    ]

    major_min, minor_min = required_wine_minimum
    if wine_release:
        major = wine_release.major
        minor = wine_release.minor
        release_type = wine_release.release
        result = True, "None"  # Whether the release is allowed; error message
        for rule in rules:
            if major == rule.major:
                # Verify release is allowed
                if release_type not in rule.allowed_releases:
                    if minor >= (rule.devel_allowed or float('inf')):
                        if release_type not in ["staging", "devel"]:
                            result = (
                                False,
                                (
                                    f"Wine release needs to be devel or staging. "
                                    f"Current release: {release_type}."
                                )
                            )
                            break
                    else:
                        result = (
                            False,
                            (
                                f"Wine release needs to be {rule.allowed_releases}. "  # noqa: E501
                                f"Current release: {release_type}."
                            )
                        )
                        break
                # Verify version is allowed
                if minor in rule.minor_bad:
                    result = False, f"Wine version {major}.{minor} will not work."
                    break
                if major < major_min:
                    result = (
                        False,
                        (
                            f"Wine version {major}.{minor} is "
                            f"below minimum required ({major_min}.{minor_min}).")
                    )
                    break
                elif major == major_min and minor < minor_min:
                    if not rule.proton:
                        result = (
                            False,
                            (
                                f"Wine version {major}.{minor} is "
                                f"below minimum required ({major_min}.{minor_min}).")  # noqa: E501
                        )
                        break
        logging.debug(f"Result: {result}")
        return result
    else:
        return True, "Default to trusting user override"


def check_wine_version_and_branch(release_version: Optional[str], test_binary,
                                  faithlife_product_version):
    if not os.path.exists(test_binary):
        reason = "Binary does not exist."
        return False, reason

    if not os.access(test_binary, os.X_OK):
        reason = "Binary is not executable."
        return False, reason

    wine_release, error_message = get_wine_release(test_binary)

    if wine_release is None:
        return False, error_message

    result, message = check_wine_rules(
        wine_release,
        release_version,
        faithlife_product_version
    )
    if not result:
        return result, message

    if wine_release.major > 9:
        pass

    return True, "None"


def initializeWineBottle(wine64_binary: str, app: App) -> Optional[subprocess.Popen[bytes]]: #noqa: E501
    app.status("Initializing wine bottle…")
    logging.debug(f"{wine64_binary=}")
    # Avoid wine-mono window
    wine_dll_override="mscoree="
    logging.debug(f"Running: {wine64_binary} wineboot --init")
    process = run_wine_proc(
        wine64_binary,
        app=app,
        exe='wineboot',
        exe_args=['--init'],
        init=True,
        additional_wine_dll_overrides=wine_dll_override
    )
    return process


def set_win_version(app: App, exe: str, windows_version: str):
    if exe == "logos":
        # This operation is equivilent to f"winetricks -q settings {windows_version}"
        # but faster
        process = run_wine_proc(
            app.conf.wine_binary,
            app,
            exe_args=('winecfg', '/v', windows_version)
        )
        if process:
            process.wait()

    elif exe == "indexer":
        reg = f"HKCU\\Software\\Wine\\AppDefaults\\{app.conf.faithlife_product}Indexer.exe"  # noqa: E501
        exe_args = [
            'add',
            reg,
            "/v", "Version",
            "/t", "REG_SZ",
            "/d", f"{windows_version}", "/f",
            ]
        process = run_wine_proc(
            app.conf.wine_binary,
            app,
            exe='reg',
            exe_args=exe_args
        )
        if process is None:
            app.exit("Failed to spawn command to set windows version for indexer")
        process.wait()


def wine_reg_install(app: App, name: str, reg_text: str, wine64_binary: str):
    with tempfile.TemporaryDirectory() as tempdir:
        reg_file = Path(tempdir) / name
        reg_file.write_text(reg_text)
        app.status(f"Installing registry file: {reg_file}")  
        try:
            process = run_wine_proc(
                wine64_binary,
                app=app,
                exe="regedit.exe",
                exe_args=[str(reg_file)]
            )
            if process is None:
                app.exit("Failed to spawn command to install reg file")
            process.wait()
            if process is None or process.returncode != 0:
                failed = "Failed to install reg file"
                logging.debug(f"{failed}. {process=}")
                app.exit(f"{failed}: {reg_file}")
            elif process.returncode == 0:
                logging.info(f"{reg_file} installed.")
            wineserver_wait(app)
        finally:
            reg_file.unlink()


def disable_winemenubuilder(app: App, wine64_binary: str):
    name='disable-winemenubuilder.reg'
    reg_text = r'''REGEDIT4

[HKEY_CURRENT_USER\Software\Wine\DllOverrides]
"winemenubuilder.exe"=""
'''
    wine_reg_install(app, name, reg_text, wine64_binary)


def set_renderer(app: App, wine64_binary: str, value: str):
    name=f'set-renderer-to-{value}.reg'
    reg_text = rf'''REGEDIT4

[HKEY_CURRENT_USER\Software\Wine\Direct3D]
"renderer"="{value}"
'''
    wine_reg_install(app, name, reg_text, wine64_binary)


def set_fontsmoothing_to_rgb(app: App, wine64_binary: str):
    # Possible registry values:
    # "disable":      FontSmoothing=0; FontSmoothingOrientation=1; FontSmoothingType=0
    # "gray/grey":    FontSmoothing=2; FontSmoothingOrientation=1; FontSmoothingType=1
    # "bgr":          FontSmoothing=2; FontSmoothingOrientation=0; FontSmoothingType=2
    # "rgb":          FontSmoothing=2; FontSmoothingOrientation=1; FontSmoothingType=2
    # https://github.com/Winetricks/winetricks/blob/8cf82b3c08567fff6d3fb440cbbf61ac5cc9f9aa/src/winetricks#L17411

    name='set-fontsmoothing-to-rgb.reg'
    reg_text = r'''REGEDIT4

[HKEY_CURRENT_USER\Control Panel\Desktop]
"FontSmoothing"="2"
"FontSmoothingGamma"=dword:00000578
"FontSmoothingOrientation"=dword:00000001
"FontSmoothingType"=dword:00000002
'''
    wine_reg_install(app, name, reg_text, wine64_binary)


def install_msi(app: App):
    app.status(f"Running MSI installer: {app.conf.faithlife_installer_name}.")
    # Define the Wine executable and initial arguments for msiexec
    wine_exe = app.conf.wine64_binary
    exe_args = ["/i", f"{app.conf.install_dir}/data/{app.conf.faithlife_installer_name}"]  # noqa: E501

    # Add passive mode if specified
    if app.conf._overrides.faithlife_install_passive is True:
        # Ensure the user agrees to the EULA. Exit if they don't.
        if (
            app.conf._overrides.agreed_to_faithlife_terms or
            app.approve_or_exit("Do you agree to Faithlife's EULA? https://faithlife.com/terms")
        ):
            exe_args.append("/passive")

    # Add MST transform if needed
    release_version = app.conf.installed_faithlife_product_release or app.conf.faithlife_product_version  # noqa: E501
    if release_version is not None and utils.check_logos_release_version(release_version, 39, 1): #noqa: E501
        # Define MST path and transform to windows path.
        mst_path = constants.APP_ASSETS_DIR / "LogosStubFailOK.mst"
        # FIXME: move this to run_wine_proc after types are cleaner
        transform_winpath = subprocess.run(
            [wine_exe, 'winepath', '-w', mst_path],
            env=system.fix_ld_library_path(get_wine_env(app)),
            capture_output=True,
            text=True,
        ).stdout.rstrip()
        exe_args.append(f'TRANSFORMS={transform_winpath}')
        logging.debug(f"TRANSFORMS windows path added: {transform_winpath}")

    # Log the msiexec command and run the process
    logging.info(f"Running: {wine_exe} msiexec {' '.join(exe_args)}")
    return run_wine_proc(wine_exe, app, exe="msiexec", exe_args=exe_args)


def get_winecmd_encoding(app: App) -> Optional[str]:
    # Get wine system's cmd.exe encoding for proper decoding to UTF8 later.
    logging.debug("Getting wine system's cmd.exe encoding.")
    registry_value = get_registry_value(
        'HKCU\\Software\\Wine\\Fonts',
        'Codepages',
        app
    )
    if registry_value is not None:
        codepages: str = registry_value.split(',')
        return codepages[-1]
    else:
        m = "wine.wine_proc: wine.get_registry_value returned None."
        logging.error(m)
        return None


def run_wine_proc(
    winecmd,
    app: App,
    exe=None,
    exe_args=list(),
    init=False,
    additional_wine_dll_overrides: Optional[str] = None
) -> Optional[subprocess.Popen[bytes]]:
    logging.debug("Getting wine environment.")
    env = get_wine_env(app, additional_wine_dll_overrides)
    if isinstance(winecmd, Path):
        winecmd = str(winecmd)
    logging.debug(f"run_wine_proc: {winecmd}; {exe=}; {exe_args=}")

    command = [winecmd]
    if exe is not None:
        command.append(exe)
    if exe_args:
        command.extend(exe_args)

    cmd = f"subprocess cmd: '{' '.join(command)}'"
    logging.debug(cmd)
    try:
        with open(app.conf.app_wine_log_path, 'a') as wine_log:
            print(f"{utils.get_timestamp()}: {cmd}", file=wine_log)
            return system.popen_command(
                command,
                stdout=wine_log,
                stderr=wine_log,
                env=env,
                start_new_session=True,
                encoding='utf-8'
            )

    except subprocess.CalledProcessError as e:
        logging.error(f"Exception running '{' '.join(command)}': {e}")
    return None


# FIXME: Consider when to re-run this if it changes.
# Perhaps we should have a "apply installation updates"
# or similar mechanism to ensure all of our latest methods are installed
# including but not limited to: system packages, icu files, fonts, registry
# edits, etc.
#
# Seems like we want to have a more holistic mechanism for ensuring
# all users use the latest and greatest.
# Sort of like an update, but for wine and all of the bits underneath "Logos" itself
def enforce_icu_data_files(app: App):
    app.status("Downloading ICU files…")
    icu_url = app.conf.icu_latest_version_url
    icu_latest_version = app.conf.icu_latest_version

    icu_filename = os.path.basename(icu_url).removesuffix(".tar.gz")
    # Append the version to the file name so it doesn't collide with previous versions
    icu_filename = f"{icu_filename}-{icu_latest_version}.tar.gz"
    network.logos_reuse_download(
        icu_url,
        icu_filename,
        app.conf.download_dir,
        app=app
    )

    app.status("Copying ICU files…")

    drive_c = f"{app.conf.wine_prefix}/drive_c"
    utils.untar_file(f"{app.conf.download_dir}/{icu_filename}", drive_c)

    # Ensure the target directory exists
    icu_win_dir = f"{drive_c}/icu-win/windows"
    if not os.path.exists(icu_win_dir):
        os.makedirs(icu_win_dir)

    shutil.copytree(icu_win_dir, f"{drive_c}/windows", dirs_exist_ok=True)
    app.status("ICU files copied.", 100)



def get_registry_value(reg_path, name, app: App):
    logging.debug(f"Get value for: {reg_path=}; {name=}")
    # FIXME: consider breaking run_wine_proc into a helper function before decoding is attempted # noqa: E501
    # NOTE: Can't use run_wine_proc here because of infinite recursion while
    # trying to determine wine_output_encoding.
    value = None
    env = get_wine_env(app)

    cmd = [
        app.conf.wine64_binary,
        'reg', 'query', reg_path, '/v', name,
    ]
    err_msg = f"Failed to get registry value: {reg_path}\\{name}"
    encoding = app.conf._wine_output_encoding
    if encoding is None:
        encoding = 'UTF-8'
    try:
        result = system.run_command(
            cmd,
            encoding=encoding,
            env=env
        )
    except subprocess.CalledProcessError as e:
        if 'non-zero exit status' in str(e):
            logging.warning(err_msg)
            return None
    if result is not None and result.stdout is not None:
        for line in result.stdout.splitlines():
            if line.strip().startswith(name):
                value = line.split()[-1].strip()
                logging.debug(f"Registry value: {value}")
                break
    else:
        logging.critical(err_msg)
    return value


def get_wine_env(app: App, additional_wine_dll_overrides: Optional[str]=None) -> dict[str, str]: #noqa: E501
    wine_env = os.environ.copy()
    winepath = Path(app.conf.wine_binary)
    if winepath.name != 'wine64':  # AppImage
        winepath = Path(app.conf.wine64_binary)
    wine_env_defaults = {
        'WINE': str(winepath),
        'WINEDEBUG': app.conf.wine_debug,
        'WINEDLLOVERRIDES': app.conf.wine_dll_overrides,
        'WINELOADER': str(winepath),
        'WINEPREFIX': app.conf.wine_prefix,
        'WINESERVER': app.conf.wineserver_binary,
    }
    for k, v in wine_env_defaults.items():
        wine_env[k] = v

    if additional_wine_dll_overrides is not None:
        wine_env["WINEDLLOVERRIDES"] += ";" + additional_wine_dll_overrides # noqa: E501

    updated_env = {k: wine_env.get(k) for k in wine_env_defaults.keys()}
    logging.debug(f"Wine env: {updated_env}")
    # Extra safe calling this here, it should be called run run_command anyways
    return system.fix_ld_library_path(wine_env)
