from pyinfra.operations import apt

from roles.bash import bash
from roles.user import user

bash.apply()
user.apply()

if __name__ == "__main__":
    print("uv run pyinfra inventory.py deploy.py")
