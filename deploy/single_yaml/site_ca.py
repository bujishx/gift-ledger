"""A private installation authority and a TLS server certificate for one LAN IP."""

import ipaddress
from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


PROFILE = "site-ca-v1"
ROOT_DAYS = 1825
LEAF_DAYS = 90
PEM = serialization.Encoding.PEM


def private_pem(key):
    return key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def certificate_info(host, leaf, root):
    return {
        "profile": PROFILE, "host": host, "validity_days": LEAF_DAYS,
        "not_before": leaf.not_valid_before_utc.isoformat(timespec="seconds"),
        "not_after": leaf.not_valid_after_utc.isoformat(timespec="seconds"),
        "certificate_sha256": leaf.fingerprint(hashes.SHA256()).hex(),
        "root_sha256": root.fingerprint(hashes.SHA256()).hex(),
    }


def issue(host, instance, now):
    ip = ipaddress.IPv4Address(host)
    start = now - timedelta(minutes=5)
    issuer_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"GiftLedger Site CA {instance}")])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    root = (
        x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer)
        .public_key(issuer_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + timedelta(days=ROOT_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, None, None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(issuer_key.public_key()), critical=False)
        .add_extension(x509.NameConstraints([x509.IPAddress(ipaddress.ip_network(f"{ip}/32"))], None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
        .public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + timedelta(days=LEAF_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, None, None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
        .sign(issuer_key, hashes.SHA256())
    )
    files = {
        "certs/fullchain.pem": leaf.public_bytes(PEM),
        "certs/privkey.pem": private_pem(leaf_key),
        "certs/ca-chain.pem": root.public_bytes(PEM),
    }
    return files, certificate_info(host, leaf, root), private_pem(issuer_key)


def verify(files, issuer_key_pem, host, now, expected):
    root = x509.load_pem_x509_certificate(files["certs/ca-chain.pem"])
    leaf = x509.load_pem_x509_certificate(files["certs/fullchain.pem"])
    issuer_key = serialization.load_pem_private_key(issuer_key_pem, password=None)
    leaf_key = serialization.load_pem_private_key(files["certs/privkey.pem"], password=None)
    ip = ipaddress.IPv4Address(host)
    root.verify_directly_issued_by(root)
    leaf.verify_directly_issued_by(root)
    root_ext = root.extensions.get_extension_for_class
    leaf_ext = leaf.extensions.get_extension_for_class
    if not (
        isinstance(issuer_key, rsa.RSAPrivateKey) and issuer_key.key_size == 3072
        and isinstance(leaf_key, rsa.RSAPrivateKey) and leaf_key.key_size == 2048
        and issuer_key.public_key() == root.public_key() and leaf_key.public_key() == leaf.public_key()
        and root_ext(x509.BasicConstraints).value == x509.BasicConstraints(True, 0)
        and root_ext(x509.KeyUsage).value.key_cert_sign
        and root_ext(x509.NameConstraints).value == x509.NameConstraints([x509.IPAddress(ipaddress.ip_network(f"{ip}/32"))], None)
        and leaf_ext(x509.BasicConstraints).value == x509.BasicConstraints(False, None)
        and not leaf_ext(x509.KeyUsage).value.key_cert_sign
        and leaf_ext(x509.ExtendedKeyUsage).value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH])
        and leaf_ext(x509.SubjectAlternativeName).value == x509.SubjectAlternativeName([x509.IPAddress(ip)])
        and root.not_valid_before_utc <= leaf.not_valid_before_utc <= now
        and now + timedelta(days=1) < leaf.not_valid_after_utc <= root.not_valid_after_utc
        and leaf.not_valid_after_utc - leaf.not_valid_before_utc == timedelta(days=LEAF_DAYS)
        and certificate_info(host, leaf, root) == expected
    ):
        raise ValueError("site_ca_verification_failed")
