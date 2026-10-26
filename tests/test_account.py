from __future__ import annotations
import importlib.util, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'skill/windows-vm-sandbox/scripts/account.py'
PROFILE=ROOT/'skill/windows-vm-sandbox/assets/profiles/winvm01.example.json'

def load():
    spec=importlib.util.spec_from_file_location('account_tested',SCRIPT); mod=importlib.util.module_from_spec(spec); assert spec.loader is not None
    ps=importlib.util.spec_from_file_location('profile',ROOT/'skill/windows-vm-sandbox/scripts/profile.py'); pm=importlib.util.module_from_spec(ps); assert ps.loader is not None; ps.loader.exec_module(pm); sys.modules['profile']=pm
    spec.loader.exec_module(mod); return mod

class AccountCliTests(unittest.TestCase):
    def test_dry_run_does_not_invoke_ssh_or_read_secret(self):
        m=load()
        with tempfile.TemporaryDirectory() as td:
            profile=Path(td)/'profile.json'; profile.write_text(PROFILE.read_text(encoding='utf-8'),encoding='utf-8')
            sys.argv=['account.py','--profile',str(profile),'--admin-user','kaijie','--credentials-mode','file','--secret-file','/root/private/secret.json']
            with patch('subprocess.run',side_effect=AssertionError('dry-run must not invoke ssh')):
                self.assertEqual(m.main(),0)

    def test_password_never_appears_in_ssh_argv(self):
        m=load(); data=json.loads(PROFILE.read_text(encoding='utf-8'))
        self.assertNotIn('password',m._ssh(data))

    def test_actual_bundled_remote_payload_compiles(self):
        m=load()
        def execute(argv,**kwargs):
            compile(kwargs['input'],'remote-account','exec')
            self.assertNotIn('__file__',kwargs['input'])
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch.object(sys,'argv',['account.py','--profile',str(PROFILE),'--admin-user','alice','--credentials-mode','generate','--apply']),patch.object(m.subprocess,'run',side_effect=execute):
            self.assertEqual(m.main(),0)

    def test_account_update_falls_back_to_setpassword_for_an_administrator(self):
        # The WinNT ADSI provider refuses ChangePassword when an administrator
        # changes another account's password; SetPassword is the way out.
        helper=(ROOT/'skill/windows-vm-sandbox/assets/templates/account-update.ps1').read_text(encoding='utf-8-sig')
        self.assertRegex(helper,r"try\{[^}]*ChangePassword[^}]*\}catch\{[^}]*SetPassword")
        self.assertIn('$adsi.SetPassword([string]$r.password)',helper)


if __name__=='__main__': unittest.main()
