"""Test the installer's Steam account pick and its rerun after an install that
stopped partway, with a fake Steam, a fake GitHub and mocked macOS tools.

Run: python3 tests/test-installer.py
A real Steam download needs a native macOS smoke test.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = os.environ.get("PACKAGING_TEST_BASH") or shutil.which("bash")
BASE = 76561197960265728  # a SteamID64 is BASE + the account id
KID, PARENT = str(BASE + 39734273), str(BASE + 39734274)  # fake accounts

# Stand-in for GitHub: answers the release lookup, then the tarball download.
CURL = r'''out="" url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -w) shift 2;;
    -*) shift;;
    *) url="$1"; shift;;
  esac
done
case "$url" in
  */releases/latest) cp "$TEST_RELEASE_JSON" "$out"; printf 200;;
  *.tar.gz) cp "$TEST_TARBALL" "$out";;
  *) exit 22;;
esac'''


def shell_path(path):
    value = str(path).replace("\\", "/")
    if os.name == "nt" and len(value) > 1 and value[1] == ":":
        value = "/" + value[0].lower() + value[2:]
    return value


def vdf(tree, depth=0):
    # Steam's text VDF, laid out as Steam writes it.
    tab, out = "\t" * depth, ""
    for key, value in tree.items():
        if isinstance(value, dict):
            out += f'{tab}"{key}"\n{tab}{{\n{vdf(value, depth + 1)}{tab}}}\n'
        else:
            out += f'{tab}"{key}"\t\t"{value}"\n'
    return out


class Sandbox(unittest.TestCase):
    """A fake home and mocked tools, so nothing reaches the real Steam."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(
            prefix="installer-", dir=os.environ.get("PACKAGING_TEST_TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / "fake home"
        self.steam = self.home / "Library/Application Support/Steam"
        self.mockbin = self.base / "mockbin"
        self.mockbin.mkdir()
        self.mock("python3", 'exec "$TEST_PYTHON" "$@"')
        self.mock("pgrep", "exit 1")  # neither Steam nor the game is running
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("INSANIQ_")}
        self.env.update(HOME=shell_path(self.home), TEST_PYTHON=shell_path(sys.executable),
                        TMPDIR=shell_path(self.base), PYTHONDONTWRITEBYTECODE="1")
        if os.name == "nt":
            self.env["MSYS"] = "winsymlinks:sys"

    def mock(self, name, body):
        target = self.mockbin / name
        target.write_text("#!/bin/bash\n" + body + "\n")
        target.chmod(0o755)

    def run_script(self, script, *args):
        # $BASH: the scripts run under the same bash as this shell.
        return subprocess.run(
            [BASH, "-c", 'export PATH="$1:$PATH"; shift; exec "$BASH" "$@"', "test",
             shell_path(self.mockbin), script, *args],
            cwd=ROOT, env=self.env, text=True, stdin=subprocess.DEVNULL, check=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class SteamAccountTests(Sandbox):
    """inject.sh names the account the game files download under (LastOwner).
    1.2.0 stopped at step 3 on any Mac where two Steam accounts had signed in,
    because current Steam no longer flags the one that signed in last."""

    def setUp(self):
        super().setUp()
        (self.steam / "appcache").mkdir(parents=True)
        (self.steam / "appcache/appinfo.vdf").write_text("fixture")
        (self.steam / "steamapps").mkdir()
        self.acf = self.steam / "steamapps/appmanifest_3320.acf"

    def signed_in(self, users):
        # loginusers.vdf as current Steam writes it: no MostRecent flag.
        (self.steam / "config").mkdir(exist_ok=True)
        (self.steam / "config/loginusers.vdf").write_text(vdf({"users": users}))

    def kid_and_parent(self, kid_ts="1764100000", parent_ts="1763000000", **parent):
        self.signed_in({
            KID: {"AccountName": "kid_one", "PersonaName": "Kid", "timestamp": kid_ts},
            PARENT: dict({"AccountName": "parent_one", "PersonaName": "Parent",
                          "timestamp": parent_ts}, **parent),
        })

    def auto_login(self, account):
        (self.steam / "registry.vdf").write_text(vdf({"Registry": {"HKCU": {"Software": {
            "Valve": {"Steam": {"AutoLoginUser": account}}}}}}))

    def inject(self, phase="pending", *args):
        return self.run_script("scripts/steam-play-button/inject.sh", "--phase", phase, *args)

    def assert_owner(self, result, steamid, why):
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(self.acf.read_text(), rf'"LastOwner"\s+"{steamid}"')
        self.assertIn(f"({why})", result.stdout)

    def assert_stopped(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stdout)
        self.assertFalse(self.acf.exists())

    def test_two_accounts_use_the_one_steam_signs_in_to(self):
        # The reported failure. Steam matches account names case-insensitively.
        self.kid_and_parent()
        self.auto_login("Parent_One")
        result = self.inject()
        self.assert_owner(result, PARENT, "Steam signs in to it automatically")
        self.assertIn("Steam account: Parent (", result.stdout)

    def test_two_accounts_without_auto_login_use_the_newest_sign_in(self):
        self.kid_and_parent()
        self.assert_owner(self.inject(), KID, "it signed in to Steam last")

    def test_auto_login_for_a_forgotten_account_is_skipped(self):
        self.kid_and_parent()
        self.auto_login("someone_else")
        self.assert_owner(self.inject(), KID, "it signed in to Steam last")

    def test_older_steam_most_recent_flag_still_counts(self):
        self.kid_and_parent(MostRecent="1")
        self.assert_owner(self.inject(), PARENT, "it signed in to Steam last")

    def test_only_account_shows_its_name_unescaped(self):
        self.signed_in({KID: {"AccountName": "kid_one", "PersonaName": 'Kid \\"Q\\"'}})
        result = self.inject()
        self.assert_owner(result, KID, "the only account signed in to Steam on this Mac")
        self.assertIn('Steam account: Kid "Q" (', result.stdout)

    def test_no_account_list_uses_the_newest_userdata(self):
        for steamid, used in ((KID, 1764100000), (PARENT, 1763000000)):
            config = self.steam / "userdata" / str(int(steamid) - BASE) / "config/localconfig.vdf"
            config.parent.mkdir(parents=True)
            config.write_text("fixture")
            os.utime(config, (used, used))
        (self.steam / "userdata/anonymous").mkdir()
        self.assert_owner(self.inject(), KID, "it used Steam last")

    def test_steam_id_from_the_environment_wins(self):
        self.kid_and_parent()
        self.env["INSANIQ_STEAMID"] = PARENT
        self.assert_owner(self.inject(), PARENT, "set by INSANIQ_STEAMID")

    def test_owner_option_beats_the_environment(self):
        self.env["INSANIQ_STEAMID"] = PARENT
        self.assert_owner(self.inject("pending", "--owner", KID), KID, "set by --owner")

    def test_a_wrong_steam_id_stops_before_writing(self):
        self.env["INSANIQ_STEAMID"] = "12345"
        self.assert_stopped(self.inject(), "INSANIQ_STEAMID=12345 is not a Steam ID")

    def test_undecidable_stops_with_what_to_do(self):
        self.kid_and_parent(parent_ts="1764100000")  # same sign-in time
        self.assert_stopped(self.inject(), "INSANIQ_STEAMID=<your Steam ID> ./setup.sh")
        self.env["INSANIQ_RERUN"] = "paste the install command into Terminal again"
        self.assert_stopped(self.inject(), "| INSANIQ_STEAMID=<your Steam ID> bash")

    def test_finalize_writes_the_same_account(self):
        # Without a finished appmanifest from Steam, finalize writes its own.
        self.kid_and_parent()
        result = self.inject("finalize")
        self.assert_owner(result, KID, "it signed in to Steam last")
        self.assertRegex(self.acf.read_text(), r'"StateFlags"\s+"4"')


class UnfinishedInstallTests(Sandbox):
    """Pasting the install command again after an install stopped partway.
    1.2.0 counted the app alone as installed and recorded the release at step
    1, so it answered "already up to date" and did nothing."""

    TAG = "1.2.1"

    def setUp(self):
        super().setUp()
        self.mock("sw_vers", "echo 14.0")
        self.mock("xcode-select", "exit 0")
        self.mock("curl", CURL)
        self.apps = self.base / "Applications"
        self.app = self.apps / "Insaniquarium.app"
        self.porthome = self.home / "Library/Application Support/Insaniquarium-port"
        self.porthome.mkdir(parents=True)
        self.log = self.base / "setup-args"
        dist = self.base / "dist/insaniquarium-mac-dist"
        (dist / "payload/Insaniquarium.app").mkdir(parents=True)
        setup = dist / "setup.sh"
        setup.write_text('#!/bin/bash\necho "setup.sh $*" > "$TEST_LOG"\n')
        setup.chmod(0o755)
        tarball = self.base / f"insaniquarium-mac-{self.TAG}.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(dist, arcname=dist.name)
        release = self.base / "release.json"
        release.write_text(json.dumps({"tag_name": self.TAG, "assets": [{
            "name": tarball.name,
            "browser_download_url": f"http://127.0.0.1:9/o/r/releases/download/{self.TAG}/{tarball.name}",
        }]}))
        # Unreachable addresses: a run that missed the curl mock fails fast.
        self.env.update(
            INSANIQ_TEST_RELEASE_API="http://127.0.0.1:9/releases/latest",
            INSANIQ_TEST_ASSET_HOST="http://127.0.0.1:9",
            INSANIQ_TEST_APPS_DIR=shell_path(self.apps),
            TEST_RELEASE_JSON=shell_path(release), TEST_TARBALL=shell_path(tarball),
            TEST_LOG=shell_path(self.log))

    def installed(self, release, game_files):
        binary = self.app / "Contents/MacOS/insaniquarium"
        binary.parent.mkdir(parents=True)
        binary.write_text("fixture")
        binary.chmod(0o755)
        if game_files:
            resources = self.app / "Contents/Resources"
            for name in ("images", "sounds", "music", "fishsongs", "data", "properties"):
                (resources / name).mkdir(parents=True)
            (resources / "properties/resources.xml").write_text("fixture")
        (self.porthome / "RELEASE").write_text(release + "\n")

    def install_command(self, *args):
        result = self.run_script("scripts/steam-play-button/update.sh", *args)
        setup = self.log.read_text().strip() if self.log.exists() else None
        return result, setup

    def assert_starts_over(self, release):
        self.installed(release, game_files=False)
        result, setup = self.install_command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("the last install didn't finish", result.stdout)
        self.assertEqual(setup, "setup.sh")  # a first install, not --update

    def test_unfinished_install_starts_the_setup_over(self):
        # The reported lockout: release recorded, game files never arrived.
        self.assert_starts_over(self.TAG)

    def test_install_stuck_on_1_2_0_starts_the_setup_over(self):
        # Before, the new release took the update path and stopped there,
        # finding no game files to keep.
        self.assert_starts_over("1.2.0")

    def test_finished_install_is_up_to_date(self):
        self.installed(self.TAG, game_files=True)
        result, setup = self.install_command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"already up to date ({self.TAG})", result.stdout)
        self.assertIsNone(setup)

    def test_finished_older_install_updates(self):
        self.installed("1.2.0", game_files=True)
        result, setup = self.install_command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(setup, "setup.sh --update")

    def test_in_app_updater_leaves_an_unfinished_install_alone(self):
        self.installed("1.2.0", game_files=False)
        result, setup = self.install_command("--yes")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("a first install has to run in Terminal", result.stdout)
        self.assertNotIn("didn't finish", result.stdout)
        self.assertIsNone(setup)

    def test_setup_records_the_release_only_after_the_game_files(self):
        # Step 1 installs the helper scripts. The release is recorded after
        # step 5, so an install that stops in between isn't taken as finished.
        checkout = self.base / "checkout"
        shutil.copytree(ROOT / "scripts/steam-play-button", checkout / "scripts/steam-play-button")
        (checkout / "RELEASE").write_text(self.TAG + "\n")
        definitions = (ROOT / "setup.sh").read_text().split('\nCMD=install ASSETS=""', 1)[0]
        fixture = checkout / "fixture.sh"
        fixture.write_text(definitions + "\ninstall_scripts\n")
        result = self.run_script(shell_path(fixture))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.porthome / "inject.sh").is_file())
        self.assertFalse((self.porthome / "RELEASE").exists())
        fixture.write_text(definitions + "\nrecord_release\n")
        result = self.run_script(shell_path(fixture))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((self.porthome / "RELEASE").read_text().strip(), self.TAG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
