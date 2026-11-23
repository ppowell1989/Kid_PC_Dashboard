"""Usage: python -m app.hashpw 'password'  -> prints a hash for config.yaml"""
import sys

from .security import hash_password

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m app.hashpw 'password'")
    print(hash_password(sys.argv[1]))
