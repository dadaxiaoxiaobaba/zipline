#
# Copyright 2013 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Tests for the zipline.finance package
"""
from datetime import datetime, timedelta
import os
from unittest import TestCase


from nose.tools import timed
import numpy as np
import pandas as pd
import pytz
from six.moves import range
from testfixtures import TempDirectory

from zipline.finance.blotter import Blotter
from zipline.finance.execution import MarketOrder, LimitOrder
from zipline.finance.trading import TradingEnvironment
from zipline.finance.performance import PerformanceTracker
from zipline.finance.trading import SimulationParameters
from zipline.testing import (
    setup_logger,
    teardown_logger
)
from zipline.data.us_equity_pricing import BcolzDailyBarReader
from zipline.data.minute_bars import BcolzMinuteBarReader

from zipline.data.data_portal import DataPortal
from zipline.finance.slippage import FixedSlippage
from zipline.protocol import BarData
from zipline.testing.core import write_bcolz_minute_data
from .utils.daily_bar_writer import DailyBarWriterFromDataFrames

import zipline.utils.factory as factory

DEFAULT_TIMEOUT = 15  # seconds
EXTENDED_TIMEOUT = 90

_multiprocess_can_split_ = False


class FinanceTestCase(TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env = TradingEnvironment()
        cls.env.write_data(equities_identifiers=[1, 2, 133])

    @classmethod
    def tearDownClass(cls):
        del cls.env

    def setUp(self):
        self.zipline_test_config = {
            'sid': 133,
        }

        setup_logger(self)

    def tearDown(self):
        teardown_logger(self)

    # TODO: write tests for short sales
    # TODO: write a test to do massive buying or shorting.

    @timed(DEFAULT_TIMEOUT)
    def test_partially_filled_orders(self):

        # create a scenario where order size and trade size are equal
        # so that orders must be spread out over several trades.
        params = {
            'trade_count': 360,
            'trade_interval': timedelta(minutes=1),
            'order_count': 2,
            'order_amount': 100,
            'order_interval': timedelta(minutes=1),
            # because we placed two orders for 100 shares each, and the volume
            # of each trade is 100, and by default you can take up 2.5% of the
            # bar's volume, the simulator should spread the order into 100
            # trades of 2 shares per order.
            'expected_txn_count': 100,
            'expected_txn_volume': 2 * 100,
            'default_slippage': True
        }

        self.transaction_sim(**params)

        # same scenario, but with short sales
        params2 = {
            'trade_count': 360,
            'trade_interval': timedelta(minutes=1),
            'order_count': 2,
            'order_amount': -100,
            'order_interval': timedelta(minutes=1),
            'expected_txn_count': 100,
            'expected_txn_volume': 2 * -100,
            'default_slippage': True
        }

        self.transaction_sim(**params2)

    @timed(DEFAULT_TIMEOUT)
    def test_collapsing_orders(self):
        # create a scenario where order.amount <<< trade.volume
        # to test that several orders can be covered properly by one trade,
        # but are represented by multiple transactions.
        params1 = {
            'trade_count': 6,
            'trade_interval': timedelta(hours=1),
            'order_count': 24,
            'order_amount': 1,
            'order_interval': timedelta(minutes=1),
            # because we placed an orders totaling less than 25% of one trade
            # the simulator should produce just one transaction.
            'expected_txn_count': 24,
            'expected_txn_volume': 24
        }
        self.transaction_sim(**params1)

        # second verse, same as the first. except short!
        params2 = {
            'trade_count': 6,
            'trade_interval': timedelta(hours=1),
            'order_count': 24,
            'order_amount': -1,
            'order_interval': timedelta(minutes=1),
            'expected_txn_count': 24,
            'expected_txn_volume': -24
        }
        self.transaction_sim(**params2)

        # Runs the collapsed trades over daily trade intervals.
        # Ensuring that our delay works for daily intervals as well.
        params3 = {
            'trade_count': 6,
            'trade_interval': timedelta(days=1),
            'order_count': 24,
            'order_amount': 1,
            'order_interval': timedelta(minutes=1),
            'expected_txn_count': 24,
            'expected_txn_volume': 24
        }
        self.transaction_sim(**params3)

    @timed(DEFAULT_TIMEOUT)
    def test_alternating_long_short(self):
        # create a scenario where we alternate buys and sells
        params1 = {
            'trade_count': int(6.5 * 60 * 4),
            'trade_interval': timedelta(minutes=1),
            'order_count': 4,
            'order_amount': 10,
            'order_interval': timedelta(hours=24),
            'alternate': True,
            'complete_fill': True,
            'expected_txn_count': 4,
            'expected_txn_volume': 0  # equal buys and sells
        }
        self.transaction_sim(**params1)

    def transaction_sim(self, **params):
        """ This is a utility method that asserts expected
        results for conversion of orders to transactions given a
        trade history"""
        tempdir = TempDirectory()
        try:
            trade_count = params['trade_count']
            trade_interval = params['trade_interval']
            order_count = params['order_count']
            order_amount = params['order_amount']
            order_interval = params['order_interval']
            expected_txn_count = params['expected_txn_count']
            expected_txn_volume = params['expected_txn_volume']

            # optional parameters
            # ---------------------
            # if present, alternate between long and short sales
            alternate = params.get('alternate')

            # if present, expect transaction amounts to match orders exactly.
            complete_fill = params.get('complete_fill')

            env = TradingEnvironment()

            sid = 1

            if trade_interval < timedelta(days=1):
                sim_params = factory.create_simulation_parameters(
                    data_frequency="minute"
                )

                minutes = env.market_minute_window(
                    sim_params.first_open,
                    int((trade_interval.total_seconds() / 60) * trade_count)
                    + 100)

                price_data = np.array([10.1] * len(minutes))
                assets = {
                    sid: pd.DataFrame({
                        "open": price_data,
                        "high": price_data,
                        "low": price_data,
                        "close": price_data,
                        "volume": np.array([100] * len(minutes)),
                        "dt": minutes
                    }).set_index("dt")
                }

                write_bcolz_minute_data(
                    env,
                    env.days_in_range(minutes[0], minutes[-1]),
                    tempdir.path,
                    assets
                )

                equity_minute_reader = BcolzMinuteBarReader(tempdir.path)

                data_portal = DataPortal(
                    env,
                    equity_minute_reader=equity_minute_reader,
                )
            else:
                sim_params = factory.create_simulation_parameters(
                    data_frequency="daily"
                )

                days = sim_params.trading_days

                assets = {
                    1: pd.DataFrame({
                        "open": [10.1] * len(days),
                        "high": [10.1] * len(days),
                        "low": [10.1] * len(days),
                        "close": [10.1] * len(days),
                        "volume": [100] * len(days),
                        "day": [day.value for day in days]
                    }, index=days)
                }

                path = os.path.join(tempdir.path, "testdata.bcolz")
                DailyBarWriterFromDataFrames(assets).write(
                    path, days, assets)

                equity_daily_reader = BcolzDailyBarReader(path)

                data_portal = DataPortal(
                    env,
                    equity_daily_reader=equity_daily_reader,
                )

            if "default_slippage" not in params or \
               not params["default_slippage"]:
                slippage_func = FixedSlippage()
            else:
                slippage_func = None

            blotter = Blotter(sim_params.data_frequency, self.env.asset_finder,
                              slippage_func)

            env.write_data(equities_data={
                sid: {
                    "start_date": sim_params.trading_days[0],
                    "end_date": sim_params.trading_days[-1]
                }
            })

            start_date = sim_params.first_open

            if alternate:
                alternator = -1
            else:
                alternator = 1

            tracker = PerformanceTracker(sim_params, self.env)

            # replicate what tradesim does by going through every minute or day
            # of the simulation and processing open orders each time
            if sim_params.data_frequency == "minute":
                ticks = minutes
            else:
                ticks = days

            transactions = []

            order_list = []
            order_date = start_date
            for tick in ticks:
                blotter.current_dt = tick
                if tick >= order_date and len(order_list) < order_count:
                    # place an order
                    direction = alternator ** len(order_list)
                    order_id = blotter.order(
                        blotter.asset_finder.retrieve_asset(sid),
                        order_amount * direction,
                        MarketOrder())
                    order_list.append(blotter.orders[order_id])
                    order_date = order_date + order_interval
                    # move after market orders to just after market next
                    # market open.
                    if order_date.hour >= 21:
                        if order_date.minute >= 00:
                            order_date = order_date + timedelta(days=1)
                            order_date = order_date.replace(hour=14, minute=30)
                else:
                    bar_data = BarData(
                        data_portal,
                        lambda: tick,
                        sim_params.data_frequency
                    )
                    txns, _ = blotter.get_transactions(bar_data)
                    for txn in txns:
                        tracker.process_transaction(txn)
                        transactions.append(txn)

            for i in range(order_count):
                order = order_list[i]
                self.assertEqual(order.sid, sid)
                self.assertEqual(order.amount, order_amount * alternator ** i)

            if complete_fill:
                self.assertEqual(len(transactions), len(order_list))

            total_volume = 0
            for i in range(len(transactions)):
                txn = transactions[i]
                total_volume += txn.amount
                if complete_fill:
                    order = order_list[i]
                    self.assertEqual(order.amount, txn.amount)

            self.assertEqual(total_volume, expected_txn_volume)

            self.assertEqual(len(transactions), expected_txn_count)

            cumulative_pos = tracker.position_tracker.positions[sid]
            if total_volume == 0:
                self.assertIsNone(cumulative_pos)
            else:
                self.assertEqual(total_volume, cumulative_pos.amount)

            # the open orders should not contain sid.
            oo = blotter.open_orders
            self.assertNotIn(sid, oo, "Entry is removed when no open orders")
        finally:
            tempdir.cleanup()

    def test_blotter_processes_splits(self):
        blotter = Blotter('daily', self.env.asset_finder,
                          slippage_func=FixedSlippage())

        # set up two open limit orders with very low limit prices,
        # one for sid 1 and one for sid 2
        blotter.order(
            blotter.asset_finder.retrieve_asset(1), 100, LimitOrder(10))
        blotter.order(
            blotter.asset_finder.retrieve_asset(2), 100, LimitOrder(10))

        # send in a split for sid 2
        blotter.process_splits([(2, 0.3333)])

        for sid in [1, 2]:
            order_lists = blotter.open_orders[sid]
            self.assertIsNotNone(order_lists)
            self.assertEqual(1, len(order_lists))

        aapl_order = blotter.open_orders[1][0].to_dict()
        fls_order = blotter.open_orders[2][0].to_dict()

        # make sure the aapl order didn't change
        self.assertEqual(100, aapl_order['amount'])
        self.assertEqual(10, aapl_order['limit'])
        self.assertEqual(1, aapl_order['sid'])

        # make sure the fls order did change
        # to 300 shares at 3.33
        self.assertEqual(300, fls_order['amount'])
        self.assertEqual(3.33, fls_order['limit'])
        self.assertEqual(2, fls_order['sid'])


class TradingEnvironmentTestCase(TestCase):
    """
    Tests for date management utilities in zipline.finance.trading.
    """

    def setUp(self):
        setup_logger(self)

    def tearDown(self):
        teardown_logger(self)

    @classmethod
    def setUpClass(cls):
        cls.env = TradingEnvironment()

    @classmethod
    def tearDownClass(cls):
        del cls.env

    @timed(DEFAULT_TIMEOUT)
    def test_is_trading_day(self):
        # holidays taken from: http://www.nyse.com/press/1191407641943.html
        new_years = datetime(2008, 1, 1, tzinfo=pytz.utc)
        mlk_day = datetime(2008, 1, 21, tzinfo=pytz.utc)
        presidents = datetime(2008, 2, 18, tzinfo=pytz.utc)
        good_friday = datetime(2008, 3, 21, tzinfo=pytz.utc)
        memorial_day = datetime(2008, 5, 26, tzinfo=pytz.utc)
        july_4th = datetime(2008, 7, 4, tzinfo=pytz.utc)
        labor_day = datetime(2008, 9, 1, tzinfo=pytz.utc)
        tgiving = datetime(2008, 11, 27, tzinfo=pytz.utc)
        christmas = datetime(2008, 5, 25, tzinfo=pytz.utc)
        a_saturday = datetime(2008, 8, 2, tzinfo=pytz.utc)
        a_sunday = datetime(2008, 10, 12, tzinfo=pytz.utc)
        holidays = [
            new_years,
            mlk_day,
            presidents,
            good_friday,
            memorial_day,
            july_4th,
            labor_day,
            tgiving,
            christmas,
            a_saturday,
            a_sunday
        ]

        for holiday in holidays:
            self.assertTrue(not self.env.is_trading_day(holiday))

        first_trading_day = datetime(2008, 1, 2, tzinfo=pytz.utc)
        last_trading_day = datetime(2008, 12, 31, tzinfo=pytz.utc)
        workdays = [first_trading_day, last_trading_day]

        for workday in workdays:
            self.assertTrue(self.env.is_trading_day(workday))

    def test_simulation_parameters(self):
        env = SimulationParameters(
            period_start=datetime(2008, 1, 1, tzinfo=pytz.utc),
            period_end=datetime(2008, 12, 31, tzinfo=pytz.utc),
            capital_base=100000,
            env=self.env,
        )

        self.assertTrue(env.last_close.month == 12)
        self.assertTrue(env.last_close.day == 31)

    @timed(DEFAULT_TIMEOUT)
    def test_sim_params_days_in_period(self):

        #     January 2008
        #  Su Mo Tu We Th Fr Sa
        #         1  2  3  4  5
        #   6  7  8  9 10 11 12
        #  13 14 15 16 17 18 19
        #  20 21 22 23 24 25 26
        #  27 28 29 30 31

        params = SimulationParameters(
            period_start=datetime(2007, 12, 31, tzinfo=pytz.utc),
            period_end=datetime(2008, 1, 7, tzinfo=pytz.utc),
            capital_base=100000,
            env=self.env,
        )

        expected_trading_days = (
            datetime(2007, 12, 31, tzinfo=pytz.utc),
            # Skip new years
            # holidays taken from: http://www.nyse.com/press/1191407641943.html
            datetime(2008, 1, 2, tzinfo=pytz.utc),
            datetime(2008, 1, 3, tzinfo=pytz.utc),
            datetime(2008, 1, 4, tzinfo=pytz.utc),
            # Skip Saturday
            # Skip Sunday
            datetime(2008, 1, 7, tzinfo=pytz.utc)
        )

        num_expected_trading_days = 5
        self.assertEquals(num_expected_trading_days, params.days_in_period)
        np.testing.assert_array_equal(expected_trading_days,
                                      params.trading_days.tolist())

    @timed(DEFAULT_TIMEOUT)
    def test_market_minute_window(self):

        #     January 2008
        #  Su Mo Tu We Th Fr Sa
        #         1  2  3  4  5
        #   6  7  8  9 10 11 12
        #  13 14 15 16 17 18 19
        #  20 21 22 23 24 25 26
        #  27 28 29 30 31

        us_east = pytz.timezone('US/Eastern')
        utc = pytz.utc

        # 10:01 AM Eastern on January 7th..
        start = us_east.localize(datetime(2008, 1, 7, 10, 1))
        utc_start = start.astimezone(utc)

        # Get the next 10 minutes
        minutes = self.env.market_minute_window(
            utc_start, 10,
        )
        self.assertEqual(len(minutes), 10)
        for i in range(10):
            self.assertEqual(minutes[i], utc_start + timedelta(minutes=i))

        # Get the previous 10 minutes.
        minutes = self.env.market_minute_window(
            utc_start, 10, step=-1,
        )
        self.assertEqual(len(minutes), 10)
        for i in range(10):
            self.assertEqual(minutes[i], utc_start + timedelta(minutes=-i))

        # Get the next 900 minutes, including utc_start, rolling over into the
        # next two days.
        # Should include:
        # Today:    10:01 AM  ->  4:00 PM  (360 minutes)
        # Tomorrow: 9:31  AM  ->  4:00 PM  (390 minutes, 750 total)
        # Last Day: 9:31  AM  -> 12:00 PM  (150 minutes, 900 total)
        minutes = self.env.market_minute_window(
            utc_start, 900,
        )
        today = self.env.market_minutes_for_day(start)[30:]
        tomorrow = self.env.market_minutes_for_day(
            start + timedelta(days=1)
        )
        last_day = self.env.market_minutes_for_day(
            start + timedelta(days=2))[:150]

        self.assertEqual(len(minutes), 900)
        self.assertEqual(minutes[0], utc_start)
        self.assertTrue(all(today == minutes[:360]))
        self.assertTrue(all(tomorrow == minutes[360:750]))
        self.assertTrue(all(last_day == minutes[750:]))

        # Get the previous 801 minutes, including utc_start, rolling over into
        # Friday the 4th and Thursday the 3rd.
        # Should include:
        # Today:    10:01 AM -> 9:31 AM (31 minutes)
        # Friday:   4:00 PM  -> 9:31 AM (390 minutes, 421 total)
        # Thursday: 4:00 PM  -> 9:41 AM (380 minutes, 801 total)
        minutes = self.env.market_minute_window(
            utc_start, 801, step=-1,
        )

        today = self.env.market_minutes_for_day(start)[30::-1]
        # minus an extra two days from each of these to account for the two
        # weekend days we skipped
        friday = self.env.market_minutes_for_day(
            start + timedelta(days=-3),
        )[::-1]
        thursday = self.env.market_minutes_for_day(
            start + timedelta(days=-4),
        )[:9:-1]

        self.assertEqual(len(minutes), 801)
        self.assertEqual(minutes[0], utc_start)
        self.assertTrue(all(today == minutes[:31]))
        self.assertTrue(all(friday == minutes[31:421]))
        self.assertTrue(all(thursday == minutes[421:]))

    def test_min_date(self):
        min_date = pd.Timestamp('2016-03-04', tz='UTC')
        env = TradingEnvironment(min_date=min_date)

        self.assertGreaterEqual(env.first_trading_day, min_date)
        self.assertGreaterEqual(env.treasury_curves.index[0],
                                min_date)

    def test_max_date(self):
        max_date = pd.Timestamp('2008-08-01', tz='UTC')
        env = TradingEnvironment(max_date=max_date)

        self.assertLessEqual(env.last_trading_day, max_date)
        self.assertLessEqual(env.treasury_curves.index[-1],
                             max_date)
