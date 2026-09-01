"""Jednorázové vygenerování NaCl klíčů pro tajné tikety.

Privátní klíč (data/secret_key.txt, gitignored!) má jen bookmaker — bot jím
tikety dešifruje. Veřejný klíč (data/public_key.txt) se zabuduje do stránky,
kde jím prohlížeč tikety pečetí. Klíče NIKDY neměnit, dokud existují
nezapečetěné tikety — starým kódům by přestal rozumět bot.
"""

import pathlib

from nacl.public import PrivateKey

DATA = pathlib.Path(__file__).parent / "data"
SECRET = DATA / "secret_key.txt"
PUBLIC = DATA / "public_key.txt"


def main() -> None:
    if SECRET.exists():
        raise SystemExit("data/secret_key.txt už existuje — klíče neměnit!")
    key = PrivateKey.generate()
    SECRET.write_text(bytes(key).hex() + "\n")
    SECRET.chmod(0o600)
    PUBLIC.write_text(bytes(key.public_key).hex() + "\n")
    print("Klíče vygenerovány: data/secret_key.txt (tajný!), data/public_key.txt")


if __name__ == "__main__":
    main()
