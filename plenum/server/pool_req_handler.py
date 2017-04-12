import json
from binascii import unhexlify
from functools import lru_cache
from typing import Tuple, List

from ledger.serializers.json_serializer import JsonSerializer
from plenum.common.exceptions import UnauthorizedClientRequest
from plenum.common.ledger import Ledger
from plenum.persistence.util import txnsWithSeqNo
from stp_core.common.log import getlogger
from plenum.common.request import Request
from plenum.common.state import PruningState
from plenum.common.constants import TXN_TYPE, NODE, TARGET_NYM, DATA, ROLE, STEWARD, \
    ALIAS, NODE_IP, NODE_PORT, CLIENT_IP, CLIENT_PORT
from plenum.common.txn_util import reqToTxn
from plenum.common.types import f
from plenum.server.domain_req_handler import DomainRequestHandler
from plenum.server.req_handler import RequestHandler

logger = getlogger()


class PoolRequestHandler(RequestHandler):

    def __init__(self, ledger: Ledger, state: PruningState,
                 domainState: PruningState):
        super().__init__(ledger, state)
        self.domainState = domainState
        self.stateSerializer = JsonSerializer()

    def validate(self, req: Request, config=None):
        typ = req.operation.get(TXN_TYPE)
        error = None
        if typ == NODE:
            nodeNym = req.operation.get(TARGET_NYM)
            if self.getNodeData(nodeNym, isCommitted=False):
                error = self.authErrorWhileUpdatingNode(req)
            else:
                error = self.authErrorWhileAddingNode(req)
        if error:
            raise UnauthorizedClientRequest(req.identifier, req.reqId,
                                            error)

    def apply(self, req: Request):
        typ = req.operation.get(TXN_TYPE)
        if typ == NODE:
            txn = reqToTxn(req)
            (start, end), _ = self.ledger.appendTxns([txn])
            self.updateState(txnsWithSeqNo(start, end, [txn]))
            return txn
        else:
            logger.debug('Cannot apply request of type {} to state'.format(typ))
            return None

    def updateState(self, txns, isCommitted=False):
        for txn in txns:
            nodeNym = txn.get(TARGET_NYM)
            data = txn.get(DATA, {})
            existingData = self.getNodeData(nodeNym, isCommitted=isCommitted)
            existingData.update(data)
            self.updateNodeData(nodeNym, existingData)

    def authErrorWhileAddingNode(self, request):
        origin = request.identifier
        operation = request.operation
        isSteward = self.isSteward(origin, isCommitted=False)
        if not isSteward:
            return "{} is not a steward so cannot add a new node".format(origin)
        if self.stewardHasNode(origin):
            return "{} already has a node".format(origin)
        if self.isNodeDataConflicting(operation.get(DATA, {}),
                                      isCommitted=False):
            return "existing data has conflicts with " \
                   "request data {}".format(operation.get(DATA))

    def authErrorWhileUpdatingNode(self, request):
        # Check if steward of the node is updating it and its data does not
        # conflict with any existing node's data
        origin = request.identifier
        operation = request.operation
        isSteward = self.isSteward(origin, isCommitted=False)
        if not isSteward:
            return "{} is not a steward so cannot update a node".format(origin)
        nodeNym = operation.get(TARGET_NYM)
        if not self.isStewardOfNode(origin, nodeNym):
            return "{} is not a steward of node {}".format(origin, nodeNym)
        if self.isNodeDataConflicting(operation.get(DATA, {}), nodeNym,
                                      isCommitted=False):
            return "existing data has conflicts with " \
                   "request data {}".format(operation.get(DATA))

    def getNodeData(self, nym, isCommitted: bool = True):
        key = nym.encode()
        data = self.state.get(key, isCommitted)
        return json.loads(data.decode()) if data else {}

    def updateNodeData(self, nym, data):
        key = nym.encode()
        val = self.stateSerializer.serialize(data)
        self.state.set(key, val)

    def isSteward(self, nym, isCommitted: bool = True):
        return DomainRequestHandler.isSteward(self.domainState, nym, isCommitted)

    @lru_cache(maxsize=64)
    def isStewardOfNode(self, stewardNym, nodeNym):
        for txn in self.ledger.getAllTxn().values():
            if txn[TXN_TYPE] == NODE and \
                            txn[TARGET_NYM] == nodeNym and \
                            txn[f.IDENTIFIER.nm] == stewardNym:
                return True
        return False

    def stewardHasNode(self, stewardNym):
        # Cannot use lru_cache since a steward might have a node in future and
        # unfortunately lru_cache does not allow single entries to be cleared
        # TODO: Modify lru_cache to clear certain entities
        for txn in self.ledger.getAllTxn().values():
            if txn[TXN_TYPE] == NODE and txn[f.IDENTIFIER.nm] == stewardNym:
                return True
        return False

    def isNodeDataConflicting(self, data, nodeNym=None, isCommitted=True):
        for txn in self.ledger.getAllTxn().values():
            if txn[TXN_TYPE] == NODE and \
                    (not nodeNym or nodeNym != txn[TARGET_NYM]):
                existingData = self.getNodeData(txn[TARGET_NYM],
                                                isCommitted=isCommitted)
                for (ip, port) in [(NODE_IP, NODE_PORT), (CLIENT_IP, CLIENT_PORT)]:
                    if (existingData.get(ip), existingData.get(port)) == (data.get(ip), data.get(port)):
                        return True
                if existingData.get(ALIAS) == data.get(ALIAS):
                    return True
