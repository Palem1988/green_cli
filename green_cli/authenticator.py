import base64
import json
import logging
import os
import stat

from typing import Dict, List

import hwilib.commands
import click

import greenaddress as gdk


try:
    import wallycore as wally
except ImportError as e:
    wally = None
    logging.warning("Failed to import wallycore: %s", e)


class Authenticator:
    """Provide authentication"""

    def login(self, session_obj):
        return gdk.login(session_obj, self.hw_device, self.mnemonic, self.password)

    def register(self, session):
        return gdk.register_user(session, self.hw_device, self.mnemonic)


class MnemonicOnDisk:
    """Persist a mnemonic using the filesystem"""

    def __init__(self, config_dir):
        self.mnemonic_filename = os.path.join(config_dir, 'mnemonic')

    @property
    def _mnemonic(self):
        return open(self.mnemonic_filename).read()

    @_mnemonic.setter
    def _mnemonic(self, mnemonic):
        """Write mnemonic to config file"""
        try:
            logging.debug('opening mnemonic file: %s', self.mnemonic_filename)
            open(self.mnemonic_filename, 'w').write(mnemonic)
        except PermissionError:
            message = (
                "Refusing to overwrite mnemonic file {}\n"
                "First backup and then delete or change file permissions"
                .format(self.mnemonic_filename))
            raise click.ClickException(message)

        # Set permissions on the mnemonic file to avoid accidental deletion
        os.chmod(self.mnemonic_filename, stat.S_IRUSR)


class SoftwareAuthenticator(Authenticator, MnemonicOnDisk):
    """Represent a 'software signer' which passes the mnemonic to the gdk for authentication
    """

    @property
    def hw_device(self):
        return json.dumps({})

    @property
    def mnemonic(self):
        return self._mnemonic

    @property
    def password(self):
        return ''

    def create(self, session_obj):
        """Create and register a new wallet"""
        self._mnemonic = gdk.generate_mnemonic()
        return self.register(session_obj)

    def setmnemonic(self, mnemonic):
        mnemonic = ' '.join(mnemonic.split())
        logging.debug("mnemonic: '{}'".format(mnemonic))
        if not gdk.validate_mnemonic(mnemonic):
            raise click.ClickException("Invalid mnemonic")
        self._mnemonic = mnemonic


class DefaultAuthenticator(SoftwareAuthenticator):
    """Adds pin login functionality"""

    def __init__(self, config_dir):
        super().__init__(config_dir)
        self.pin_data_filename = os.path.join(config_dir, 'pin_data')

    def login(self, session_obj):
        """Perform login with either mnemonic or pin data from local storage"""
        try:
            return super().login(session_obj)
        except IOError:
            try:
                pin_data = open(self.pin_data_filename).read()
            except IOError:
                print("Login failed, please call create")
                raise
            pin = input("PIN: ")
            return gdk.login_with_pin(session_obj, pin, pin_data)

    def setpin(self, session, pin, device_id):
        # session.set_pin converts the pin_data string into a dict, which is not what we want, so
        # use the underlying call instead
        print("mnemnic: {}".format(self.mnemonic))
        print("pin: {}".format(pin))
        print("device_id: {}".format(device_id))
        pin_data = gdk.set_pin(session.session_obj, self.mnemonic, pin, device_id)
        open(self.pin_data_filename, 'w').write(pin_data)
        os.remove(self.mnemonic_filename)
        return pin_data


class HardwareDevice(Authenticator):
    """Represents what the gdk refers to as a 'hardware device'.

    Not necessarily an actual hardware device, but anything that implements the required hardware
    device interface
    """

    @property
    def hw_device(self):
        return json.dumps({'device': {'name': self.name}})

    @property
    def mnemonic(self):
        return ''

    @property
    def password(self):
        return ''

    def resolve(self, details):
        """Resolve a requested action using the device"""
        logging.debug("%s resolving %s", self.name, details)
        details = details['required_data']
        if details['action'] == 'get_xpubs':
            xpubs = []
            paths = details['paths']
            logging.debug('get_xpubs paths = %s', paths)
            for path in paths:
                xpub = self.get_xpub(path)
                logging.debug('xpub for path %s: %s', path, xpub)
                xpubs.append(xpub)
            response = json.dumps({'xpubs': xpubs})
            logging.debug('resolving: %s', response)
            return response
        if details['action'] == 'sign_message':
            logging.debug('sign message path = %s', details['path'])
            message = details['message']
            logging.debug('signing message "%s"', message)
            signature = self.sign_message(details['path'], message)
            signature_hex = wally.hex_from_bytes(signature)
            result = json.dumps({'signature': signature_hex})
            logging.debug('resolving %s', result)
            return result
        if details['action'] == 'sign_tx':
            return self.sign_tx(details)
        raise NotImplementedError("action = \"{}\"".format(details['action']))


class WallyAuthenticator(MnemonicOnDisk, HardwareDevice):
    """Stores mnemonic on disk but does not pass it to the gdk

    This class illustrates how the hardware device interface to the gdk can be used to implement all
    required crypto operations external to the gdk and thus avoid passing any key material to the
    gdk at all.
    """

    @property
    def name(self):
        return 'libwally software signer'

    def create(self, session_obj):
        """Create and register a new wallet"""
        logging.warning("Generating mnemonic using gdk")
        self._mnemonic = gdk.generate_mnemonic()
        return self.register(session_obj)

    @property
    def master_key(self):
        _, seed = wally.bip39_mnemonic_to_seed512(self._mnemonic, None)
        return wally.bip32_key_from_seed(seed, wally.BIP32_VER_TEST_PRIVATE,
                                         wally.BIP32_FLAG_KEY_PRIVATE)

    def derive_key(self, path: List[int]):
        if not path:
            return self.master_key
        else:
            return wally.bip32_key_from_parent_path(self.master_key, path,
                                                    wally.BIP32_FLAG_KEY_PRIVATE)

    def get_xpub(self, path: List[int]):
        return wally.bip32_key_to_base58(self.derive_key(path), wally.BIP32_FLAG_KEY_PUBLIC)

    def get_privkey(self, path: List[int]) -> bytearray:
        return wally.bip32_key_get_priv_key(self.derive_key(path))

    def sign_message(self, path: List[int], message: str) -> bytearray:
        message = message.encode('utf-8')
        formatted = wally.format_bitcoin_message(message, wally.BITCOIN_MESSAGE_FLAG_HASH)
        privkey = self.get_privkey(path)
        signature = wally.ec_sig_from_bytes(privkey, formatted,
                                            wally.EC_FLAG_ECDSA | wally.EC_FLAG_GRIND_R)
        return wally.ec_sig_to_der(signature)

    def sign_tx(self, details):
        txdetails = details['transaction']

        utxos = txdetails['used_utxos'] or txdetails['old_used_utxos']
        signatures = []
        for index, utxo in enumerate(utxos):
            wally_tx = wally.tx_from_hex(txdetails['transaction'], wally.WALLY_TX_FLAG_USE_WITNESS)
            is_segwit = utxo['script_type'] in [14, 15, 159, 162] # FIXME!!
            if not is_segwit:
                # FIXME
                raise NotImplementedError("Non-segwit input")
            flags = wally.WALLY_TX_FLAG_USE_WITNESS if is_segwit else 0
            prevout_script = wally.hex_to_bytes(utxo['prevout_script'])
            txhash = wally.tx_get_btc_signature_hash(
                wally_tx, index, prevout_script, utxo['satoshi'], wally.WALLY_SIGHASH_ALL, flags)

            path = utxo['user_path']
            privkey = self.get_privkey(path)
            signature = wally.ec_sig_from_bytes(privkey, txhash,
                                                wally.EC_FLAG_ECDSA | wally.EC_FLAG_GRIND_R)
            signature = wally.ec_sig_to_der(signature)
            signature.append(wally.WALLY_SIGHASH_ALL)
            signatures.append(wally.hex_from_bytes(signature))
            logging.debug('Signature (der) input %s path %s: %s', index, path, signature)

        return json.dumps({'signatures': signatures})


class HWIDevice(HardwareDevice):

    @staticmethod
    def _path_to_string(path: List[int]) -> str:
        """Return string representation of path for hwi interface

        The gdk passes paths as lists of int, hwi expects strings with '/'s
        >>> _path_to_string([1, 2, 3])
        "m/1/2/3"
        """
        return '/'.join(['m'] + [str(path_elem) for path_elem in path])

    def __init__(self, details: Dict):
        """Create a hardware wallet instance

        details: Details of hardware wallet as returned by hwi enumerate command
        """
        self.details = details
        self._device = hwilib.commands.find_device(details['path'])

    @property
    def name(self) -> str:
        """Return a name for the device, e.g. 'ledger@0001:0007:00'"""
        return '{}@{}'.format(self.details['type'], self.details['path'])

    def get_xpub(self, path: List[int]) -> bytes:
        """Return a base58 encoded xpub

        path: Bip32 path of xpub to return
        """
        path = HWIDevice._path_to_string(path)
        return hwilib.commands.getxpub(self._device, path)['xpub']

    def sign_message(self, path: List[int], message: str) -> bytes:
        """Return der encoded signature of a message

        path: BIP32 path of key to use for signing
        message: Message to be signed
        """
        path = HWIDevice._path_to_string(path)

        click.echo('Signing with hardware device {}'.format(self.name))
        click.echo('Please check the device for interaction')

        signature = hwilib.commands.signmessage(self._device, message, path)['signature']
        return wally.ec_sig_to_der(base64.b64decode(signature)[1:])

    def sign_tx(self, details):
        raise NotImplementedError("hwi sign tx not implemented")

    @staticmethod
    def get_device():
        """Enumerate and select a hardware wallet"""
        devices = hwilib.commands.enumerate()
        logging.debug('hwi devices: %s', devices)

        if len(devices) == 0:
            raise click.ClickException(
                "No hwi devices\n"
                "Check:\n"
                "- A device is attached\n"
                "- udev rules, device drivers, etc.\n"
                "- Cables/connections\n"
                "- The device is enabled, for example by entering a PIN\n")
        if len(devices) > 1:
            raise NotImplementedError("Device selection not implemented")

        device = devices[0]
        logging.debug('hwi device: %s', device)
        if 'error' in device:
            raise click.ClickException(
                "Error with hwi device: {}\n"
                "Check the device and activate the bitcoin app if necessary"
                .format(device['error']))
        return HWIDevice(device)
