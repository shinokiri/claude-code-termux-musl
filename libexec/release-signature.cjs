'use strict';
// Ed25519 uses Node's maintained crypto implementation, never custom crypto.
const fs = require('node:fs');
const crypto = require('node:crypto');
const [operation, keyPath, manifestPath, signaturePath] = process.argv.slice(2);
try {
  const message = fs.readFileSync(manifestPath);
  const keys = JSON.parse(fs.readFileSync(require('node:path').join(__dirname, 'release-keys.json'), 'utf8'));
  if (operation === 'sign') {
    const privateKey = crypto.createPrivateKey(fs.readFileSync(keyPath));
    if (privateKey.asymmetricKeyType !== 'ed25519') throw Error('Expected an Ed25519 signing key');
    const publicKey = crypto.createPublicKey(privateKey).export({ type: 'spki', format: 'pem' }).toString();
    const key = keys.keys.find(key => key.public_key === publicKey);
    if (!key) throw Error('Signing key is not in the pinned public key set');
    const signature = crypto.sign(null, message, privateKey).toString('base64');
    fs.writeFileSync(signaturePath, JSON.stringify({ schema: 1, algorithm: 'ed25519', key_id: key.id, signature }) + '\n');
  } else if (operation === 'verify') {
    const envelope = JSON.parse(fs.readFileSync(signaturePath, 'utf8'));
    const key = keys.keys.find(key => key.id === envelope.key_id);
    if (envelope.schema !== 1 || envelope.algorithm !== 'ed25519' || !key) throw Error('Unknown signature or signing key');
    const signature = Buffer.from(envelope.signature, 'base64');
    if (signature.length !== 64 || signature.toString('base64') !== envelope.signature ||
        !crypto.verify(null, message, key.public_key, signature)) throw Error('Invalid release signature');
  } else throw Error('Expected sign or verify');
} catch (error) {
  console.error('Release signature: ' + error.message);
  process.exitCode = 1;
}
