import pandas as pd
import numpy as np
import collections, sortedcontainers, datetime, sys
from tqdm import tqdm
from item import TickData, Snapshot, OrderData
from typing import List, Dict, Tuple
from inspect import isfunction
from constant import OrderType, Direction, Offset, Status

'''
TODO:
* consider last trade information(on event generation)
* visualization
'''

class OrderQueue:
    '''
    in the bid/ask book, there is an order book for each price, consisted of
    all the orders on this price level.
    '''
    queue: List[Tuple[OrderData, List[OrderData]]]
    next_orders: List[OrderData]

    def __init__(self):
        self.queue = []
        self.next_orders = []

    def __del__(self):
        self._consume_algo_order_list(self.next_orders, float('inf'))

    def add_order(self, order: OrderData):
        if order.is_history:
            self.queue.append([order, self.next_orders])
            self.next_orders = []
        else:
            self.next_orders.append(order)

    def _consume_algo_order_list(self, orders: List[OrderData], amount: float):
        while len(orders) > 0:
            order = orders[0]
            if amount >= order.remain():
                amount -= order.remain()
                if hasattr(order, 'callback') and isfunction(order.callback):
                    order.callback()
                orders.pop(0)
            else:
                order.traded += amount
                break

    def match_order(self, amount: float) -> float:
        '''
        match orders by given amount, return remaining amount that is not consumed.
        using FIFO algorithm currently
        '''
        while len(self.queue) > 0:
            hist_order, algo_orders = self.queue[0]
            if amount >= hist_order.remain():
                amount -= hist_order.remain()
                self._consume_algo_order_list(algo_orders, hist_order.remain())
                if len(algo_orders) > 0:
                    if len(self.queue) > 1:
                        self.queue[1][1] = algo_orders + self.queue[1][1]
                    else:
                        self.next_orders = algo_orders + self.next_orders
                self.queue.pop(0)
            else:
                hist_order.traded += amount
                self._consume_algo_order_list(algo_orders, amount)
                amount = 0
                break
        return amount

    def total_amount(self):
        def get_amount(tp):
            s = tp[0].remain()
            s += sum(map(lambda o: o.remain(), tp[1]))
            return s
        return sum(map(get_amount, self.queue))

    def history_amount(self):
        return sum(map(lambda tp: tp[0].remain(), self.queue))

    # get the total amount in gui for height calculation
    def gui_amount(self):
        algo_height = 0
        hist_height = 0
        for hist_order, algo_orders in self.queue:
            hist_height += hist_order.volume
            algo_height += sum(map(lambda o: o.volume, algo_orders))
            if algo_height < hist_height:
                algo_height = hist_height
        algo_height += sum(map(lambda o: o.volume, self.next_orders))
        return algo_height

    def cancel_data_order(self, amount: float):
        while len(self.queue) > 0:
            hist_order, algo_orders = self.queue[0]
            if amount >= hist_order.remain():
                amount -= hist_order.remain()
                if len(hist_order) > 0:
                    if len(self.queue) > 0:
                        self.queue[1][1] = hist_order + self.queue[1][1]
                    else:
                        self.next_orders = hist_order + self.next_orders
                self.queue.pop(0)
            else:
                hist_order.volume -= amount
                amount = 0
                break
        return amount

    def cancel_algo_order(self, order_id: int):
        for hist, algos in self.queue:
            for idx, order in enumerate(algos):
                if order.order_id == order_id:
                    algos.pop(idx)
                    return

class Future:
    buy_book: Dict[float, OrderQueue]
    sell_book: Dict[float, OrderQueue]

    def __init__(self, symbol: str, tick: TickData, max_depth: int):
        self.symbol = symbol
        self.max_depth = max_depth
        self.buy_book = sortedcontainers.SortedDict()
        self.sell_book = sortedcontainers.SortedDict()
        for idx in range(tick.data_depth):
            q = OrderQueue()
            q.add_order(OrderData({'volume': tick.bid_volume[idx], 'is_history': True, 'traded': 0}))
            self.buy_book[tick.bid_price[idx]] = q
            q = OrderQueue()
            q.add_order(OrderData({'volume': tick.ask_volume[idx], 'is_history': True, 'traded': 0}))
            self.sell_book[tick.ask_price[idx]] = q

    def place_order(self, order: OrderData):
        if order.volume == 0:
            return
        if order.order_type == OrderType.LIMIT:
            if order.direction == Direction.LONG and order.offset == Offset.OPEN or order.direction == Direction.SHORT and order.offset == Offset.CLOSE:
                sell_prices = list(self.sell_book.keys())
                for sp in sell_prices:
                    if sp > order.price:
                        break
                    order.volume = self.sell_book[sp].match_order(order.volume)
                    if order.volume > 0:
                        del self.sell_book[sp]
                    else:
                        break
                if order.volume > 0:
                    if order.price not in self.buy_book:
                        self.buy_book[order.price] = OrderQueue()
                    self.buy_book[order.price].add_order(order)
            elif order.direction == Direction.SHORT and order.offset == Offset.OPEN or order.direction == Direction.LONG and order.offset == Offset.CLOSE:
                buy_prices = list(reversed(self.buy_book.keys()))
                for bp in buy_prices:
                    if bp < order.price:
                        break
                    order.volume = self.buy_book[bp].match_order(order.volume)
                    if order.volume > 0:
                        del self.buy_book[bp]
                    else:
                        break
                if order.volume > 0:
                    if order.price not in self.sell_book:
                        self.sell_book[order.price] = OrderQueue()
                    self.sell_book[order.price].add_order(order)
        elif order.order_type == OrderType.MARKET:
            pass
        else:
            pass

    def cancel_data_order(self, price: float, volume: float):
        if price in self.sell_book:
            self.sell_book[price].cancel_data_order(volume)
            if self.sell_book[price].history_amount() == 0:
                del self.sell_book[price]
        if price in self.buy_book:
            self.buy_book[price].cancel_data_order(volume)
            if self.buy_book[price].history_amount() == 0:
                del self.buy_book[price]

    def cancel_order(self, order_id: int):
        order = OrderData.get_order(order_id)
        if order.price in self.sell_book:
            self.sell_book[order.price].cancel_algo_order(order_id)
            if self.sell_book[order.price].history_amount() == 0:
                del self.sell_book[order.price]
        if order.price in self.buy_book:
            self.buy_book[order.price].cancel_algo_order(order_id)
            if self.buy_book[order.price].history_amount() == 0:
                del self.buy_book[order.price]

    def snapshot(self) -> TickData:
        sps = list(self.sell_book.keys())[:5]
        bps = list(reversed(self.buy_book.keys()))[:5]
        depth = min(len(sps), len(bps), self.max_depth)
        tick = TickData()
        tick.set_data_depth(depth)
        for i in range(depth):
            tick.bid_price[i] = bps[i]
            tick.bid_volume[i] = self.buy_book[bps[i]].total_amount()
            tick.ask_price[i] = sps[i]
            tick.ask_volume[i] = self.sell_book[sps[i]].total_amount()
        return tick


class Exchange:
    futures: Dict[str, Future]

    def __init__(self, snapshot: Snapshot, max_depth: int):
        self.futures = {}
        for k in snapshot.keys():
            self.futures[k] = Future(k, snapshot[k], max_depth)

    def add_signal(self, symbol, order_type, price, volume):
        if symbol in self.futures:
            self.futures[symbol].add_signal(order_type, price, volume)
        else:
            print(f'future {symbol} not exist!')

    def place_order(self, d) -> OrderData:
        if 'is_history' not in d:
            d['is_history'] = False
        order = OrderData(d)
        order.submit_time = datetime.datetime.now()
        order.traded = 0
        order.status = Status.SUBMITTING
        symbol = order.symbol

        if symbol in self.futures:
            self.futures[symbol].place_order(order)
        else:
            print(f'future {symbol} not exist!')

        return order

    def snapshot(self) -> Snapshot:
        ss = {}
        for symbol in self.futures:
            ss[symbol] = self.futures[symbol].snapshot()
        return ss


def get_dict_from_tick(tick: TickData, type: str):
    d = {}
    if type == 'bid':
        for i in range(0, tick.data_depth):
            d[tick.bid_price[i]] = tick.bid_volume[i]
    else:
        for i in range(0, tick.data_depth):
            d[tick.ask_price[i]] = tick.ask_volume[i]
    return d


def get_tick_diff(ticks: List[TickData]):
    last_tick = ticks[0]
    data_length = len(ticks)
    tick_events = []

    for idx in tqdm(range(1, data_length), desc='generate tick diff'):
        tick = ticks[idx]

        events = []  # (time, price, direction, amount)

        last_buy_dict = get_dict_from_tick(last_tick, 'bid')
        last_sell_dict = get_dict_from_tick(last_tick, 'ask')
        buy_dict = get_dict_from_tick(tick, 'bid')
        sell_dict = get_dict_from_tick(tick, 'ask')

        if tick.bid_price[0] < last_tick.bid_price[0] or tick.bid_price[0] == last_tick.bid_price[
                0] and tick.bid_volume[0] < last_tick.bid_volume[0]:
            price = tick.bid_price[0] if tick.bid_price[0] == last_tick.bid_price[0] else float('inf')
            volume = last_tick.bid_volume[0] - tick.bid_volume[0] if tick.bid_price[0] == last_tick.bid_price[0] else 0
            for lbp in list(last_buy_dict.keys()):
                if lbp > tick.bid_price[0]:
                    volume += last_buy_dict[lbp]
                    price = min(price, lbp)
                    del last_buy_dict[lbp]
            events.append((tick.time, 'sell', price, volume))
            if tick.bid_price[0] == last_tick.bid_price[0]:
                del buy_dict[tick.bid_price[0]]
                del last_buy_dict[tick.bid_price[0]]

        if tick.ask_price[0] > last_tick.ask_price[0] or tick.ask_price[0] == last_tick.ask_price[
                0] and tick.ask_volume[0] < last_tick.ask_volume[0]:
            price = tick.ask_price[0] if tick.ask_price[0] == last_tick.ask_price[0] else -float('inf')
            volume = last_tick.ask_volume[0] - tick.ask_volume[0] if tick.ask_price[0] == last_tick.ask_price[0] else 0
            for lsp in list(last_sell_dict.keys()):
                if lsp < tick.ask_price[0]:
                    volume += last_sell_dict[lsp]
                    price = max(price, lsp)
                    del last_sell_dict[lsp]
            events.append((tick.time, 'buy', tick.ask_price[0], volume))
            if tick.ask_price[0] == last_tick.ask_price[0]:
                del sell_dict[tick.ask_price[0]]
                del last_sell_dict[tick.ask_price[0]]

        for bp in list(buy_dict.keys()):
            last_volume = 0
            if bp in last_buy_dict:
                last_volume = last_buy_dict[bp]
                del last_buy_dict[bp]
            if buy_dict[bp] > last_volume:
                events.append((tick.time, 'buy', bp, buy_dict[bp] - last_volume))
            elif buy_dict[bp] < last_volume:
                events.append((tick.time, 'cancel', bp, last_volume - buy_dict[bp]))
            del buy_dict[bp]
        for bp in last_buy_dict:
            events.append((tick.time, 'cancel', bp, last_buy_dict[bp]))

        for sp in list(sell_dict.keys()):
            last_volume = 0
            if sp in last_sell_dict:
                last_volume = last_sell_dict[sp]
                del last_sell_dict[sp]
            if sell_dict[sp] > last_volume:
                events.append((tick.time, 'sell', sp, sell_dict[sp] - last_volume))
            elif sell_dict[sp] < last_volume:
                events.append((tick.time, 'cancel', sp, last_volume - sell_dict[sp]))
            del sell_dict[sp]
        for sp in last_sell_dict:
            events.append((tick.time, 'cancel', sp, last_sell_dict[sp]))

        last_tick = tick
        tick_events.append(events)
    return tick_events
