"""Write a CA bundle that also trusts what Windows trusts, for pip and PlatformIO behind a corporate network.

Many company networks inspect HTTPS with their own root certificate. Windows trusts it (IT deploys it into
the certificate store), but pip and PlatformIO check against their own list (certifi) and fail with
"CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate". flash.ps1 and install.ps1 run this
and point PIP_CERT / REQUESTS_CA_BUNDLE / SSL_CERT_FILE at the result for their own run only.

The bundle is certifi's usual roots (from the pip in this venv) plus every certificate in Windows' ROOT
and CA stores that is trusted for server authentication. Nothing is switched off; Python just gets the
same trust list as the rest of Windows. Standard library only.

    python tools\\windows\\ca_bundle.py <output.pem>
"""
import os, ssl, sys

SERVER_AUTH = "1.3.6.1.5.5.7.3.1"


def windows_certs():
    pems = []
    for store in ("ROOT", "CA"):
        try:
            certs = ssl.enum_certificates(store)
        except (AttributeError, OSError):        # not Windows, or the store can't be read
            continue
        for der, encoding, trust in certs:
            if encoding == "x509_asn" and (trust is True or SERVER_AUTH in trust):
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    return pems


def certifi_roots():
    try:
        from pip._vendor import certifi
        with open(certifi.where(), encoding="ascii", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def main(out):
    win = windows_certs()
    if not win:
        print("no Windows certificates found: leaving the default trust alone")
        return 1
    tmp = out + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(tmp, "w", encoding="ascii", newline="\n") as f:
        f.write(certifi_roots().rstrip() + "\n")
        f.write("".join(win))
    os.replace(tmp, out)
    print(f"CA bundle: {len(win)} Windows certificates plus the usual roots -> {out}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
