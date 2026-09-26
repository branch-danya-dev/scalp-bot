"""Observe transport lifecycle and verify reconstructed books; never import snapshots into state."""
from .offline_segment import SegmentMismatch


class ReplayTransport:
    def __init__(self, replay):
        self.replay = replay
        self.workers = {}
        self.current = {}
        self.last_id = 0

    def apply(self, row, cursor):
        engine = self.replay.engine
        symbol, body = row['symbol'], row['body']
        identity = body['workerId']
        topics = tuple(body['topics'])
        allowed = {f'orderbook.{engine.config.fast_orderbook_depth}.{symbol}',
                   f'orderbook.{engine.config.deep_orderbook_depth}.{symbol}',
                   f'publicTrade.{symbol}', f'kline.1.{symbol}'}
        if not set(topics) <= allowed or len(set(topics)) != len(topics):
            raise SegmentMismatch('transport topics do not match configured symbol streams')
        worker = self.workers.get(identity)
        if worker is None:
            if body['phase'] != 'connecting' or body['attempt'] != 1 or identity <= self.last_id:
                raise SegmentMismatch('unknown or out-of-order worker')
            old = self.current.get(symbol)
            if old is not None and any(phase != 'drained' for _, phase in old['channels'].values()):
                raise SegmentMismatch('overlapping symbol workers cannot be attributed')
            if symbol not in engine.sessions:
                raise SegmentMismatch('transport worker without active session')
            handler, fast, deep = engine._market_handler(symbol)
            worker = dict(symbol=symbol, fast=fast, deep=deep, channels={})
            self.workers[identity] = worker
            self.current[symbol] = worker
            self.replay.handlers[symbol] = handler
            self.last_id = identity
        if worker['symbol'] != symbol or self.current.get(symbol) is not worker:
            raise SegmentMismatch('worker identity does not match active symbol lifetime')
        attempt, phase = body['attempt'], body['phase']
        prior = worker['channels'].get(topics)
        if phase == 'connecting':
            valid = prior is None and attempt == 1 or prior == (attempt - 1, 'drained')
            # Changing a channel's topic grouping during an active attempt would
            # otherwise let a reconnect bypass the drain requirement.
            if any(set(topics) & set(other) for other in worker['channels'] if other != topics):
                valid = False
        else:
            predecessors = {'subscription_sent': {'connecting'}, 'fault': {'connecting', 'subscription_sent'},
                'cancelled': {'connecting', 'subscription_sent'}, 'drained': {'subscription_sent', 'fault', 'cancelled'}}
            valid = prior is not None and prior[0] == attempt and prior[1] in predecessors.get(phase, set())
        if not valid:
            raise SegmentMismatch('invalid transport transition')
        if (engine._transport_book_state(worker['fast']) != body['fastState']
                or engine._transport_book_state(worker['deep']) != body['deepState']):
            raise SegmentMismatch('reconstructed transport book state differs from capture')
        cursor.append('transport', symbol, body)
        worker['channels'][topics] = (attempt, phase)

    def admit_message(self, symbol, message):
        worker = self.current.get(symbol)
        if worker is None:
            return  # Legacy component windows can use explicitly prepared state.
        matches = [phase for topics, (_, phase) in worker['channels'].items() if message['topic'] in topics]
        if len(matches) != 1 or matches[0] not in ('subscription_sent', 'fault', 'cancelled'):
            raise SegmentMismatch('market message outside active transport queue lifetime')
