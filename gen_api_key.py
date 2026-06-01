import secrets
import hashlib
import string

ALPHABET = string.ascii_letters + string.digits + string.punctuation
key = "".join(secrets.choice(ALPHABET) for _ in range(48))
key_hash = hashlib.sha512(key.encode()).hexdigest()

print(f"API Key:  {key}")
print(f"SHA-512:  {key_hash}")
print()
print("Store the API Key somewhere safe. Put the SHA-512 hash in your .env as API_KEY_HASH.")
