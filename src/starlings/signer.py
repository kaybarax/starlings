import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .state import private_dir

NAMESPACE = "starlings-receipt"
PRINCIPAL = "starlings-verifier"


class SignerError(RuntimeError):
    pass


def executable():
    found = shutil.which("ssh-keygen")
    if found is None:
        raise SignerError("ssh-keygen was not found on PATH; install OpenSSH 8.2 or newer to sign and verify receipts")
    return found


def environment():
    return {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "LC_ALL", "HOME")}


def keygen(*args, data=None):
    result = subprocess.run([executable(), *args], input=data, capture_output=True, env=environment(), check=False)
    return result


def paths(root):
    base = Path(root) / "signer-references"
    return {"directory": base, "key": base / "verifier", "public": base / "verifier.pub", "allowed": base / "allowed_signers"}


def allowed_line(public):
    fields = public.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise SignerError("verifier public key is not an Ed25519 key")
    return f'{PRINCIPAL} namespaces="{NAMESPACE}" {fields[0]} {fields[1]}\n'


def init(root):
    files = paths(root)
    executable()
    private_dir(root, "signer-references")
    created = False
    if files["key"].exists() != files["public"].exists():
        raise SignerError(f"signer key pair is incomplete in {files['directory']}; refusing to overwrite it")
    if not files["key"].exists():
        result = keygen("-q", "-t", "ed25519", "-N", "", "-C", PRINCIPAL, "-f", str(files["key"]))
        if result.returncode:
            raise SignerError("ssh-keygen could not create the signer key: " + result.stderr.decode("utf-8", "replace").strip())
        created = True
    os.chmod(files["key"], 0o600)
    line = allowed_line(files["public"].read_text(encoding="ascii"))
    if files["allowed"].exists():
        if files["allowed"].read_text(encoding="ascii") != line:
            raise SignerError(f"{files['allowed']} does not match the signer public key; refusing to overwrite it")
    else:
        descriptor = os.open(files["allowed"], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(line)
    return {"created": created, "key": str(files["key"]), "public_key": str(files["public"]), "allowed_signers": str(files["allowed"]), "fingerprint": fingerprint(root), "encrypted_at_rest": False}


def present(root):
    files = paths(root)
    return all(files[name].is_file() for name in ("key", "public", "allowed"))


def fingerprint(root):
    files = paths(root)
    if not files["public"].is_file():
        raise SignerError("no verifier signer; run starlings signer init")
    result = keygen("-l", "-E", "sha256", "-f", str(files["public"]))
    if result.returncode:
        raise SignerError("ssh-keygen could not fingerprint the signer key: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("ascii").split()[1]


def sign(root, payload):
    files = paths(root)
    if not present(root):
        raise SignerError("no verifier signer; run starlings signer init")
    result = keygen("-Y", "sign", "-f", str(files["key"]), "-n", NAMESPACE, data=payload)
    if result.returncode:
        raise SignerError("ssh-keygen could not sign the receipt: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode("ascii")


def verify(root, payload, signature):
    files = paths(root)
    if not files["allowed"].is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="verify-", dir=files["directory"]) as scratch:
        path = Path(scratch) / "receipt.sig"
        path.write_text(signature, encoding="ascii")
        result = keygen("-Y", "verify", "-f", str(files["allowed"]), "-I", PRINCIPAL, "-n", NAMESPACE, "-s", str(path), data=payload)
    return result.returncode == 0
