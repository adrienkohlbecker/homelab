from ansible.errors import AnsibleError

def mosquitto_passwd(passwd, salt=None):
    try:
        import passlib.hash
    except Exception as e:
        raise AnsibleError('mosquitto_passlib custom filter requires the passlib pip package installed')

    SALT_SIZE = 12
    ITERATIONS = 101

    passwd = passwd.encode(encoding="utf-8")
    if salt:
        salt = salt.encode(encoding="utf-8")

    digest = passlib.hash.pbkdf2_sha512.using(salt=salt, salt_size=SALT_SIZE, rounds=ITERATIONS) \
                                        .hash(passwd) \
                                        .replace("pbkdf2-sha512", "7") \
                                        .replace(".", "+")

    return digest + "=="


class FilterModule(object):
    def filters(self):
        return {
            'mosquitto_passwd': mosquitto_passwd,
        }
