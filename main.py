from pyinfra.operations import apt

from roles.bash import bash

bash.apply()

if __name__ == "__main__":
    print("uv run pyinfra inventory.py deploy.py")
