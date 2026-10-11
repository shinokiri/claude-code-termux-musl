#include <stdio.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>
#include <openssl/x509.h>
#include <openssl/x509v3.h>

int main(int argc, char **argv) {
    if (argc != 3 && argc != 4) return 2;
    EVP_PKEY_CTX *ctx = EVP_PKEY_CTX_new_id(EVP_PKEY_RSA, NULL);
    EVP_PKEY *key = NULL;
    if (!ctx || EVP_PKEY_keygen_init(ctx) <= 0 ||
        EVP_PKEY_CTX_set_rsa_keygen_bits(ctx, 2048) <= 0 ||
        EVP_PKEY_keygen(ctx, &key) <= 0) return 3;
    X509 *cert = X509_new();
    X509_set_version(cert, 2);
    ASN1_INTEGER_set(X509_get_serialNumber(cert), 1);
    X509_gmtime_adj(X509_get_notBefore(cert), -60);
    X509_gmtime_adj(X509_get_notAfter(cert), 86400);
    X509_set_pubkey(cert, key);
    X509_NAME *name = X509_get_subject_name(cert);
    X509_NAME_add_entry_by_txt(name, "CN", MBSTRING_ASC,
        (const unsigned char *)"Local Termux TLS test", -1, -1, 0);
    X509_set_issuer_name(cert, name);
    X509V3_CTX extctx;
    X509V3_set_ctx(&extctx, cert, cert, NULL, NULL, 0);
    X509_EXTENSION *ext = X509V3_EXT_conf_nid(NULL, &extctx, NID_subject_alt_name,
        argc == 4 ? argv[3] : "DNS:claude-network.invalid,DNS:localhost,IP:127.0.0.1");
    if (!ext || !X509_add_ext(cert, ext, -1)) return 4;
    X509_EXTENSION_free(ext);
    if (!X509_sign(cert, key, EVP_sha256())) return 5;
    FILE *keyfile = fopen(argv[1], "w"), *certfile = fopen(argv[2], "w");
    if (!keyfile || !certfile) return 6;
    int ok = PEM_write_PrivateKey(keyfile, key, NULL, NULL, 0, NULL, NULL) && PEM_write_X509(certfile, cert);
    fclose(keyfile); fclose(certfile);
    X509_free(cert); EVP_PKEY_free(key); EVP_PKEY_CTX_free(ctx);
    return ok ? 0 : 7;
}
