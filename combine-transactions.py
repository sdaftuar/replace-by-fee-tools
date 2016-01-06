#!/usr/bin/python3
#
# This file is subject to the license terms in the LICENSE file found in the
# top-level directory of this distribution.

import argparse
import binascii
import bitcoin
import bitcoin.rpc
import logging
import math

from bitcoin.core import b2x, b2lx, lx, str_money_value, COIN, CMutableTransaction, CMutableTxIn, CMutableTxOut
from bitcoin.wallet import CBitcoinAddress

DUST = int(0.0001 * COIN)

parser = argparse.ArgumentParser(description="Combine two transactions.")
parser.add_argument('-v', action='store_true',
                    dest='verbose',
                    help='Verbose')
parser.add_argument('-t', action='store_true',
                    dest='testnet',
                    help='Enable testnet')
parser.add_argument('-n', action='store_true',
                    dest='dryrun',
                    help="Dry-run; don't actually send the transaction")
parser.add_argument('-o', action='store_true',
                    dest='optin',
                    help='Allow me to possibly lose funds by making new tx opt-in to RBF and repeatedly using this program')
parser.add_argument('txid1', action='store', type=str,
                    help='Transaction id 1')
parser.add_argument('txid2', action='store', type=str,
                    help='Transaction id 2')
args = parser.parse_args()

if args.verbose:
    logging.root.setLevel('DEBUG')

if args.testnet:
    bitcoin.SelectParams('testnet')

rpc = bitcoin.rpc.Proxy()

def check_full_rbf_optin(tx):
    """Return true if tx opts in to full-RBF"""
    for vin in tx.vin:
        if vin.nSequence < 0xFFFFFFFF-1:
            return True
    return False

try:
    args.txid1 = lx(args.txid1)
except ValueError as err:
    parser.error('Invalid txid: %s' % str(err))

try:
    args.txid2 = lx(args.txid2)
except ValueError as err:
    parser.error('Invalid txid: %s' % str(err))

# TODO: Clean up error reporting here to indicate which transaction failed.
if len(args.txid1) != 32 or len(args.txid2) != 32:
    parser.error('Invalid txid: Wrong length.')

try:
    rpc.gettransaction(args.txid1)
    rpc.gettransaction(args.txid2)
except IndexError as err:
    parser.exit('Invalid txid: Not in wallet.')

txinfo1 = rpc.getrawtransaction(args.txid1, True)
tx1 = CMutableTransaction.from_tx(txinfo1['tx'])

txinfo2 = rpc.getrawtransaction(args.txid2, True)
tx2 = CMutableTransaction.from_tx(txinfo2['tx'])

if 'confirmations' in txinfo1 and txinfo1['confirmations'] > 0:
    parser.exit("Transaction already mined; %d confirmations." % txinfo1['confirmations'])

if 'confirmations' in txinfo2 and txinfo2['confirmations'] > 0:
    parser.exit("Transaction already mined; %d confirmations." % txinfo2['confirmations'])

# Verify that the two transactions are opting in to RBF (fail otherwise).
if not check_full_rbf_optin(tx1) or not check_full_rbf_optin(tx2):
    parser.exit("One or both transactions haven't opted in to RBF")

# TODO: Verify that all the inputs of the original transactions are coming
# from us (fail otherwise).

# Join the vout's of each transaction, except:
#  - First consolidate payments to the same address down to one.
#  - Reuse any existing payment to self as a change address.
# Drop all but first input from EACH transaction, and then use fundrawtransaction
# to select the remaining inputs. This ensures that each transaction will be replaced,
# so you don't pay everyone twice.

# IMPORTANT NOTE: If you repeat this process in the future using the transaction
# produced by this script along with some other new one, then it's possible
# the transaction you will generate in the future will NOT conflict with one
# of the two transactions used here -- meaning you could pay the same parties
# multiple times!  So default to having this replacing transaction NOT opt-in
# to RBF. Use the command line arguments to override this, but tread carefully.

outputs = {}

change_txout = []
for vout in tx1.vout + tx2.vout:
    try:
        addr = CBitcoinAddress.from_scriptPubKey(vout.scriptPubKey)
        if rpc.validateaddress(addr)['ismine']:
            change_txout.append(vout.scriptPubKey)
            print ("found change address")
            print (len(change_txout))
    except ValueError:
        pass
    try:
        outputs[vout.scriptPubKey] += vout.nValue
        print("Found duplicate output address, consolidating")
    except KeyError:
        outputs[vout.scriptPubKey] = vout.nValue

# Look up fees (easier than multiple calls to getrawtransaction to add up
# nValue for each input's prevout).
# This also allows us to incorporate any fee modifications from
# prioritisetransaction
tx1_entry = rpc.call("getmempoolentry", b2lx(args.txid1))
tx2_entry = rpc.call("getmempoolentry", b2lx(args.txid2))

tx1_fees = int(tx1_entry["modifiedfee"] * COIN)
tx2_fees = int(tx2_entry["modifiedfee"] * COIN)

print(tx1_fees, tx2_fees)

# Units: satoshi's per byte
old_fees = tx1_fees + tx2_fees
old_fees_per_byte = old_fees / (len(tx1.serialize()) + len(tx2.serialize()))

# Will ensure that the new fee will pay for all descendants.
descendants_tx1 = rpc.call('getdescendants', b2lx(args.txid1), True)
descendants_tx2 = rpc.call('getdescendants', b2lx(args.txid2), True)

descendants_tx1.update(descendants_tx2) # merge maps
descendant_fees = 0
for x in descendants_tx1.keys():
    # use modified fee, since that's what your node will use
    descendant_fees += int(descendants_tx1[x]["modifiedfee"]*COIN)

new_tx = CMutableTransaction.from_tx(tx1)
new_tx.vin = new_tx.vin[0:1] + tx2.vin[0:1]

# if tx1 or tx2 happened to spend from each other, we must drop it.
if b2lx(new_tx.vin[1].prevout.hash) in descendants_tx1.keys():
    del new_tx.vin[1]

elif b2lx(new_tx.vin[0].prevout.hash) in descendants_tx1.keys():
    del new_tx.vin[0]

new_tx.vout = []

# Add all the (consolidated) outputs, but don't add the change output.  If
# there was more than one output going to ourselves in the first two
# transactions, then this is probably not doing the optimal thing.
for x in outputs.keys():
    if x not in change_txout:
        new_tx.vout.append(CMutableTxOut(outputs[x], x))

r = rpc.fundrawtransaction(new_tx)
print(r['tx'])
new_tx = CMutableTransaction.from_tx(r['tx'])
new_tx_fee = r['fee']
changepos = r['changepos']
# TODO: handle the case where no change output was needed
assert(changepos >= 0)

for txin in new_tx.vin:
    if args.optin:
        txin.nSequence = 0
    else:
        txin.nSequence = 0xFFFFFFFF-1

r = rpc.signrawtransaction(new_tx)
assert(r['complete'])
new_tx = CMutableTransaction.from_tx(r['tx'])

min_relay_fee = rpc.call('getnetworkinfo')['relayfee'] * COIN

# Compare the new fee to what is needed.
if new_tx_fee < old_fees + descendant_fees:
    # Try to update the change address
    new_tx.vout[changepos].nValue -= old_fees + descendant_fees + 1 - new_tx_fee
    new_tx_fee = old_fees + descendant_fees

# Compare the feerate to what is needed.
new_tx_size = len(new_tx.serialize())

min_fee_rate = max(tx1_fees/len(tx1.serialize()), tx2_fees/len(tx2.serialize()))
if new_tx_fee / new_tx_size < min_fee_rate:
    d = min_fee_rate * new_tx_size - new_tx_fee
    new_tx.vout[changepos].nValue -= d
    new_tx_fee += d

# Pay for relay bandwidth of new transaction
relay_bw_fee = int(new_tx_size/1000 * float(min_relay_fee))
new_tx.vout[changepos].nValue -= relay_bw_fee
new_tx_fee += relay_bw_fee

if new_tx.vout[changepos].nValue <= 0:
    parser.exit("Error: unhandled case where unable to sufficiently bump fee on replacing transaction, exiting")

r = rpc.signrawtransaction(new_tx)
assert(r['complete'])
new_tx = r['tx']

new_tx_size = len(new_tx.serialize())

tx1_size = len(tx1.serialize())
tx2_size = len(tx2.serialize())

logging.debug('Old sizes: %.3f KB %.3f KB (%.3f KB combined), Old fees: %s, %s BTC/KB; %s, %s BTC/KB (%s, %s BTC/KB combined)' % \
         (tx1_size / 1000,
         tx2_size / 1000,
         (tx1_size + tx2_size) / 1000,
         str_money_value(tx1_fees),
         str_money_value(tx1_fees *1000 / tx1_size),
         str_money_value(tx2_fees),
         str_money_value(tx2_fees * 1000 / tx2_size),
         str_money_value(tx1_fees + tx2_fees),
         str_money_value((tx1_fees + tx2_fees) / (tx1_size + tx2_size))))

logging.debug('New tx size: %.3f KB, New fee: %s, %s BTC/KB' % \
         (new_tx_size / 1000,
         str_money_value(new_tx_fee),
         str_money_value(new_tx_fee * 1000 / new_tx_size)))

# Sanity check that new_tx replaces/conflicts with tx1 and tx2
# TODO: this isn't quite right; if tx1 and tx2 depended on each
# other, then one of these assertions could fail.
assert tx1.vin[0].prevout == new_tx.vin[0].prevout
assert tx2.vin[0].prevout == new_tx.vin[1].prevout

if args.dryrun:
    print(b2x(new_tx.serialize()))

else:
    logging.debug('Sending tx %s' % b2x(new_tx.serialize()))
    txid = rpc.sendrawtransaction(new_tx)
    print(b2lx(txid))
