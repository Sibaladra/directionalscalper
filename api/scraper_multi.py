from __future__ import annotations

import concurrent.futures
import json
import sys
import time
from datetime import datetime

import pandas as pd
import pidfile
import ta
import numpy as np

sys.path.append(".")
from directionalscalper.api.exchanges.binance import Binance
from directionalscalper.api.exchanges.bybit import Bybit
from directionalscalper.core.utils import send_public_request
from directionalscalper.core.logger import Logger
log = Logger(filename="combined_scraper.log", stream=True)

class CombinedScraper:
    def __init__(self, exchange_name, filters: dict):
        self.exchange_name = exchange_name
        if exchange_name == "binance":
            self.exchange = Binance()
        elif exchange_name == "bybit":
            self.exchange = Bybit()
        else:
            raise ValueError("Invalid exchange name provided. Use 'binance' or 'bybit'.")
        
        log.info("Scraper initializing for " + exchange_name)
        self.filters = filters
        self.symbols = self.exchange.get_futures_symbols()
        self.prices = self.exchange.get_futures_prices()
        log.info(f"{len(self.symbols)} symbols found for " + exchange_name)
        
        if "quote_symbols" in self.filters:
            self.symbols = self.filter_quote(symbols=self.symbols, quotes=self.filters["quote_symbols"])
        
        if "top_volume" in self.filters:
            self.volumes = self.exchange.get_futures_volumes()
            self.symbols = self.filter_volume(symbols=self.symbols, volumes=self.volumes, limit=self.filters["top_volume"])

    def get_historical_volume_bybit(self, symbol: str, interval: str, limit: int):
        data = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )
        return [candle["volume"] for candle in data]

    def get_historical_volume_binance(self, symbol: str, interval: str, limit: int) -> list:
        endpoint = "/fapi/v1/klines"
        payload = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        _, raw_json = send_public_request(
            url=self.exchange.futures_api_url, 
            url_path=endpoint, 
            payload=payload
        )
        # Extracting volumes from the klines data
        volumes = [entry[5] for entry in raw_json]  # Assuming volume is at index 5 in the klines data
        return volumes

    def get_all_historical_volume_bybit(self, interval: str, limit: int) -> dict:
        all_volume = {}
        for symbol in self.symbols:  # Note: changed 'scraper' to 'self'
            data = self.get_historical_volume_bybit(
                symbol=symbol, interval=interval, limit=limit
            )
            all_volume[symbol] = data
        return all_volume

    def get_all_historical_volume_binance(self, interval: str, limit: int) -> dict:
        all_volume = {}
        for symbol in self.symbols:  # Note: changed 'scraper' to 'self'
            data = self.get_historical_volume_binance(
                symbol=symbol, interval=interval, limit=limit
            )
            all_volume[symbol] = data
        return all_volume

    def output_df(self, dataframe, path: str, to: str = "json"):
        if to == "json":
            dataframe.to_json(path, orient="records", date_format='iso')
        elif to == "csv":
            dataframe.to_csv(path, index=False)
        elif to == "parquet":
            dataframe.to_parquet(path)
        elif to == "dict":
            dataframe.to_dict(path, orient="records")
        else:
            log.error(f"Output to {to} not implemented")

    def filter_df(self, dataframe, filter_col: str, operator: str, value: int):
        if operator == ">":
            return dataframe[dataframe[filter_col] > value]
        elif operator == "<":
            return dataframe[dataframe[filter_col] < value]
        elif operator == "==":
            return dataframe[dataframe[filter_col] == value]
        else:
            log.error(f"Operator {operator} not implemented")

    def reduce_df(self, dataframe, columns: list):
        return dataframe[columns]
        
    def filter_quote(self, symbols, quotes):
        log.info(f"Filtering on {len(quotes)} quote symbols")
        filtered = []
        for symbol in symbols:
            if symbol.endswith(tuple(quotes)):
                filtered.append(symbol)
        log.info(f"Filtered to {len(filtered)} symbols")
        return filtered

    def filter_volume(self, symbols, volumes, limit):
        log.info(f"Filtering top {limit} symbols by 24h volume")
        volumes = sorted(volumes.items(), key=lambda x: x[1], reverse=True)
        volumes = volumes[:limit]
        volumes = dict(volumes)
        volume_keys = [*volumes]
        filtered = []
        for symbol in symbols:
            if symbol in volume_keys:
                filtered.append(symbol)
        log.info(f"Filtered to {len(filtered)} symbols")
        return filtered

    def get_spread(
        self, symbol: str, limit: int, timeframe: str = "1m", data: list | None = None
    ):
        if data is None:
            data = self.exchange.get_futures_kline(
                symbol=symbol, interval=timeframe, limit=limit
            )
        spread = 0.0
        lowest_low = 999999
        highest_high = 0
        for d in data:
            if d["high"] > highest_high:
                highest_high = d["high"]
            if d["low"] < lowest_low:
                lowest_low = d["low"]
        if highest_high > 0:
            spread = round((highest_high - lowest_low) / highest_high * 100, 4)
        return spread

    def spread_calc(self, data):
        data["high-low"] = abs(data["high"] - abs(data["low"]))
        spread = data[["high-low"]].max(axis=1)
        return spread

    def volume_calc(self, data):
        return data[["volume"]].max(axis=1)

    def get_candle_info(self, symbol: str, timeframe: str, limit: int):
        bars = self.exchange.get_futures_kline(
            symbol=symbol, interval=timeframe, limit=limit
        )
        df = pd.DataFrame(
            bars[:-1], columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["time"] / 1000, unit="s")

        df["ema_6"] = df["close"].ewm(span=6).mean()
        df["ema_6_high"] = df["high"].ewm(span=6).mean()
        df["ema_6_low"] = df["low"].ewm(span=6).mean()

        df["spread"] = (df["ema_6_high"] - df["ema_6_low"]) / df["ema_6_low"] * 100

        return df

    def get_candle_data(self, symbol: str, interval: str, limit: int):
        bars = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )
        df = pd.DataFrame(
            bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df["MA_3_High"] = df.high.rolling(3).mean()
        df["MA_3_Low"] = df.low.rolling(3).mean()
        df["MA_6_High"] = df.high.rolling(6).mean()
        df["MA_6_Low"] = df.low.rolling(6).mean()

        return {
            "high_3": df["MA_3_High"].iat[-1],
            "low_3": df["MA_3_Low"].iat[-1],
            "high_6": df["MA_6_High"].iat[-1],
            "low_6": df["MA_6_Low"].iat[-1],
        }

    def get_hma(self, symbol: str, interval: str, limit: int, column: str, window: int):
        bars = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )
        df = pd.DataFrame(
            bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df['HMA'] = self.compute_hma(df, column, window)
        hma_order_pct = round((df[column].iloc[-1] - df['HMA'].iloc[-1]) / df[column].iloc[-1] * 100, 4)

        return hma_order_pct

    def compute_hma(self, df, column: str, window: int):
        # Step 1
        wma_half_period = df[column].rolling(window=int(window/2)).mean()

        # Step 2
        wma_full_period = df[column].rolling(window=window).mean()

        # Step 3
        series2 = 2 * wma_half_period - wma_full_period

        # Step 4 & 5
        hma = series2.rolling(window=int(np.sqrt(window))).mean()
        return hma

    def get_ema(self, symbol: str, interval: str, limit: int, column: str, window: int):
        bars = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )  # 1m, 18, 6
        df = pd.DataFrame(
            bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df[f"EMA{window} {column}"] = ta.trend.EMAIndicator(
            df[column], window=window
        ).ema_indicator()
        return round(
            (df[f"EMA{window} {column}"][limit - 1]).astype(float),
            self.symbols["price_scale"],
        )

    def get_sma(self, symbol: str, interval: str, limit: int, column: str, window: int):
        bars = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )
        df = pd.DataFrame(
            bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        sma = ta.trend.SMAIndicator(df[column], window=window).sma_indicator()

        current_sma = float(sma[limit - 1])

        last_close_price = bars[limit - 1]["close"]

        return round((last_close_price - current_sma) / last_close_price * 100, 4)

    def get_average_true_range(self, symbol: str, period, interval: str, limit: int):
        data = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )
        data["tr"] = self.get_true_range(data=data)
        atr = data["tr"].rolling(period).mean()
        return atr

    def get_true_range(self, data):
        data["previous_close"] = data["close"].shift(1)
        data["high-low"] = abs(data["high"] - data["low"])
        data["high-pc"] = abs(data["high"] - data["previous_close"])
        data["low-pc"] = abs(data["low"] - data["previous_close"])
        tr = data[["high-low", "high-pc", "low-pc"]].max(axis=1)
        return tr

    # Get MFIRSI
    def get_mfi(self, symbol: str, interval: str, limit: int, lookback: int = 200) -> str:
        bars = self.exchange.get_futures_kline(
            symbol=symbol, interval=interval, limit=limit
        )
        df = pd.DataFrame(
            bars, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )

        # Calculate MFI, RSI, MA and whether open < close
        df['mfi'] = ta.volume.MFIIndicator(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            volume=df['volume'],
            window=14,
            fillna=False
        ).money_flow_index()
        df['rsi'] = ta.momentum.rsi(df['close'], window=14)
        df['ma'] = ta.trend.sma_indicator(df['close'], window=14)
        df['open_less_close'] = (df['open'] < df['close']).astype(int)

        df['buy_condition'] = ((df['mfi'] < 20) & (df['rsi'] < 35) & (df['open_less_close'] == 1)).astype(int)
        df['sell_condition'] = ((df['mfi'] > 80) & (df['rsi'] > 65) & (df['open_less_close'] == 0)).astype(int)

        # Check the last row for whether it's a buy or sell condition
        if df.iloc[-1]['buy_condition'] == 1:
            return 'long'
        elif df.iloc[-1]['sell_condition'] == 1:
            return 'short'
        else:
            # If neither condition is met on the last bar, look back at previous bars
            for i in range(2, min(len(df), lookback) + 1):  # look back up to 'lookback' bars
                if df.iloc[-i]['buy_condition'] == 1:
                    return 'long'
                elif df.iloc[-i]['sell_condition'] == 1:
                    return 'short'
            # In case no buy or sell condition was ever met, return 'neutral'
            return 'neutral'

    # Top or bottom
    def top_or_bottom(self, df: pd.DataFrame, pd_val: int = 14, bbl: int = 20, mult: float = 2.0, 
                    lb: int = 50, n1: int = 14, n2: int = 3, ma_len: int = 50) -> pd.DataFrame:
        # ATR
        df['H-L'] = abs(df['high'] - df['low'])
        df['H-PC'] = abs(df['high'] - df['close'].shift(1))
        df['L-PC'] = abs(df['low'] - df['close'].shift(1))
        df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1, skipna=False)
        df['ATR'] = df['TR'].rolling(window=pd_val).mean()

        # Bollinger Bands
        df['MA'] = df['close'].rolling(window=bbl).mean()
        df['BB_up'] = df['MA'] + mult * df['MA'].rolling(window=bbl).std()
        df['BB_dn'] = df['MA'] - mult * df['MA'].rolling(window=bbl).std()
        df['BB_width'] = df['BB_up'] - df['BB_dn']

        # RSI
        delta = df['close'].diff()
        delta = delta[1:]
        up, down = delta.copy(), delta.copy()
        up[up < 0] = 0
        down[down > 0] = 0
        roll_up = up.rolling(window=n1).mean()
        roll_down = down.abs().rolling(window=n1).mean()
        RS = roll_up / roll_down
        df['RSI'] = 100.0 - (100.0 / (1.0 + RS))

        # Mass Index
        Range = df['high'] - df['low']
        EX1 = Range.ewm(span=9, adjust=False).mean()
        EX2 = EX1.ewm(span=9, adjust=False).mean()
        Mass = EX1 / EX2
        MassI = Mass.rolling(window=25).sum()

        # Price MA
        df['Price_MA'] = df['close'].rolling(window=ma_len).mean()

        # Define the conditions for the bottom and top
        bottom_condition = ((df['low'] < df['BB_dn']) & (MassI > 26.5) & (df['RSI'] < 30))
        top_condition = ((df['high'] > df['BB_up']) & (df['RSI'] > 70))

        # Create the bottom_buy and top_sell columns
        df['bottom_buy'] = bottom_condition
        df['top_sell'] = top_condition

        return df


    def analyse_symbol(self, symbol: str) -> dict:
        from datetime import datetime

        len_slow_ma = 64
        len_power_ema = 13
        log.info(f"Analysing: {symbol}")
        values = {"Asset": symbol}

        values["Min qty"] = self.exchange.get_symbol_info(
            symbol=symbol, info="min_order_qty"
        )

        values["Price"] = self.prices[symbol]

        candles_1h = self.exchange.get_futures_kline(
            symbol=symbol, interval="1h", limit=5
        )

        candles_30m = self.exchange.get_futures_kline(
            symbol=symbol, interval="30m", limit=5
        )
        candles_5m = self.exchange.get_futures_kline(
            symbol=symbol, interval="5m", limit=5
        )
        candles_1m = self.exchange.get_futures_kline(
            symbol=symbol, interval="1m", limit=5
        )

        data = self.exchange.get_futures_kline(symbol=symbol, interval="1m", limit=240)
        df = pd.DataFrame(
            data,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric)
        
        values["1m Spread"] = self.get_spread(symbol=symbol, limit=1, data=data[-1:])
        values["5m Spread"] = self.get_spread(symbol=symbol, limit=5, data=data[-5:])
        values["30m Spread"] = self.get_spread(symbol=symbol, limit=30, data=data[-30:])
        values["1h Spread"] = self.get_spread(symbol=symbol, limit=60, data=data[-60:])
        values["4h Spread"] = self.get_spread(symbol=symbol, limit=240, data=data)

        # Define 1x 5m candle volume
        onexcandlevol = candles_5m[-1]["volume"]
        volume_1x_5m = values["Price"] * onexcandlevol
        values["5m 1x Volume (USDT)"] = round(volume_1x_5m)

        # Define 1x 1m candle volume
        onex1mcandlevol = candles_1m[-1]["volume"]
        volume_1x = values["Price"] * onex1mcandlevol
        values["1m 1x Volume (USDT)"] = round(volume_1x)

        # Define 1x 30m candle volume
        onex30mcandlevol = candles_30m[-1]["volume"]
        volume_1x_30m = values["Price"] * onex30mcandlevol
        values["30m 1x Volume (USDT)"] = round(volume_1x_30m)

        onex1hcandlevol = candles_1h[-1]["volume"]
        volume_1x_1h = values["Price"] * onex1hcandlevol
        values["1h 1x Volume (USDT)"] = round(volume_1x_1h)

        # Define MA data
        values["5m MA6 high"] = self.get_candle_data(
            symbol=symbol, interval="5m", limit=20
        )["high_6"]
        values["5m MA6 low"] = self.get_candle_data(
            symbol=symbol, interval="5m", limit=20
        )["low_6"]

        ma_order_pct = self.get_sma(
            symbol=symbol, interval="1m", limit=30, column="close", window=14
        )
        values["trend%"] = ma_order_pct

        if ma_order_pct > 0:
            values["Trend"] = "short"
        else:
            values["Trend"] = "long"

        # Define funding rates
        values["Funding"] = self.exchange.get_funding_rate(symbol=symbol) * 100

        values["Timestamp"] = str(int(datetime.now().timestamp()))

        # Get MFI
        mfi = self.get_mfi(symbol=symbol, interval="1m", limit=200, lookback=200)
        #mfi = self.get_mfi(symbol=symbol, interval="1m", limit=100, lookback=100)
        values["MFI"] = mfi


        # Get ERI
        df["close"] = pd.to_numeric(df["close"])
        df["high"] = pd.to_numeric(df["high"])
        df["low"] = pd.to_numeric(df["low"])

        # Calculate slow EMA of closing prices
        slow_ma = df['close'].ewm(span=len_slow_ma, adjust=False).mean()

        # Determine the trend
        last_price = df['close'].values[-1]
        eri_trend = "bullish" if last_price > slow_ma.values[-1] else "bearish"

        # Calculate bull power and bear power
        bull_power = df['high'] - slow_ma
        bear_power = df['low'] - slow_ma

        # Smooth the power values using EMA
        bull_power_smoothed = bull_power.ewm(span=len_power_ema, adjust=False).mean()
        bear_power_smoothed = bear_power.ewm(span=len_power_ema, adjust=False).mean()

        # Add to the values dict
        values["ERI Bull Power"] = bull_power_smoothed.values[-1]
        values["ERI Bear Power"] = bear_power_smoothed.values[-1]
        values["ERI Trend"] = eri_trend

        # Calculate HMA trend
        hma_order_pct = self.get_hma(symbol=symbol, interval="1m", limit=30, column="close", window=14)
        values["hma_trend%"] = hma_order_pct

        #print(f"HMA ORDER PCT {hma_order_pct}")

        if hma_order_pct > 0:
            values["HMA Trend"] = "short"
        else:
            values["HMA Trend"] = "long"

        return values

    def retry_analyse_symbol(self, symbol: str, retry_limit: int):
        retry_count = 0
        while retry_count < retry_limit:
            try:
                return self.analyse_symbol(symbol)
            except Exception as e:
                retry_count += 1
                log.error(f"Exception while analysing {symbol}. Retry attempt {retry_count}. Exception: {e}")
                time.sleep(1)  # Optional: delay before retrying

        raise Exception(f"Failed to analyse {symbol} after {retry_limit} attempts.")


    def analyse_all_symbols(self, max_workers: int = 20, retry_limit: int = 5):
        data = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_data = {
                executor.submit(self.retry_analyse_symbol, symbol, retry_limit): symbol
                for symbol in self.symbols
            }
            for future in concurrent.futures.as_completed(future_data):
                symbol_data = future_data[future]
                try:
                    symbol_data_result = future.result()
                    data.append(symbol_data_result)
                except Exception as e:
                    log.error(f"{symbol_data} generated an exception: {e}")

        df = pd.DataFrame(
            data,
            columns=[
                "Asset",
                "Min qty",
                "Price",
                "1m 1x Volume (USDT)",
                "5m 1x Volume (USDT)",
                "30m 1x Volume (USDT)",
                "1h 1x Volume (USDT)",
                "1m Spread",
                "5m Spread",
                "30m Spread",
                "1h Spread",
                "4h Spread",
                "trend%",
                "Trend",
                "HMA Trend",
                "5m MA6 high",
                "5m MA6 low",
                "Funding",
                "Timestamp",
                "MFI", #OR MFIRSI
                "ERI Bull Power",
                "ERI Bear Power",
                "ERI Trend",
            ],
        )
        df.sort_values(
            by=["1m 1x Volume (USDT)", "5m Spread"],
            inplace=True,
            ascending=[False, False],
        )
        return df


    def analyse_all_symbols(self, max_workers: int = 20, retry_limit: int = 5):
        data = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_data = {
                executor.submit(self.retry_analyse_symbol, symbol, retry_limit): symbol
                for symbol in self.symbols
            }
            for future in concurrent.futures.as_completed(future_data):
                symbol_data = future_data[future]
                try:
                    symbol_data_result = future.result()
                    data.append(symbol_data_result)
                except Exception as e:
                    log.error(f"{symbol_data} generated an exception: {e}")

        df = pd.DataFrame(
            data,
            columns=[
                "Asset",
                "Min qty",
                "Price",
                "1m 1x Volume (USDT)",
                "5m 1x Volume (USDT)",
                "30m 1x Volume (USDT)",
                "1h 1x Volume (USDT)",
                "1m Spread",
                "5m Spread",
                "30m Spread",
                "1h Spread",
                "4h Spread",
                "trend%",
                "Trend",
                "HMA Trend",
                "5m MA6 high",
                "5m MA6 low",
                "Funding",
                "Timestamp",
                "MFI", #OR MFIRSI
                "ERI Bull Power",
                "ERI Bear Power",
                "ERI Trend",
            ],
        )
        df.sort_values(
            by=["1m 1x Volume (USDT)", "5m Spread"],
            inplace=True,
            ascending=[False, False],
        )
        return df


def run_scraper_for_exchange(exchange_name: str):
    log.info(f"Starting scraper for {exchange_name}")

    # User-defined parameters
    quote_symbols = ["USDT"]
    top_volume = 400
    filters = {"quote_symbols": quote_symbols, "top_volume": top_volume}

    while True:
        try:
            with pidfile.PIDFile(f"{exchange_name}_scraper.pid"):
                scraper = CombinedScraper(exchange_name=exchange_name, filters=filters)
                
                # Analyzing all symbols
                df = scraper.analyse_all_symbols()
                
                # Output the main analysis data
                output_path = f"/opt/bitnami/nginx/html/data/quantdatav2_{exchange_name}.json"
                scraper.output_df(dataframe=df, path=output_path, to="json")
                scraper.output_df(dataframe=df, path=f"data/quantdata_{exchange_name}.csv", to="csv")

                # If the exchange is bybit, save to the old path as well
                if exchange_name == "bybit":
                    scraper.output_df(dataframe=df, path="/opt/bitnami/nginx/html/data/quantdatav2.json", to="json")


                # Filter and save 'to_trade' data
                to_trade = scraper.filter_df(
                    dataframe=df,
                    filter_col="1m 1x Volume (USDT)",
                    operator=">",
                    value=15000,
                )
                scraper.output_df(
                    dataframe=to_trade, path=f"/opt/bitnami/nginx/html/data/whattotrade_{exchange_name}.csv", to="csv"
                )
                scraper.output_df(
                    dataframe=to_trade, path=f"/opt/bitnami/nginx/html/data/whattotrade_{exchange_name}.json", to="json"
                )
                
                # Filter and save 'rotator_symbols' data
                rotator_symbols = scraper.filter_df(
                    dataframe=df,
                    filter_col="1m 1x Volume (USDT)",
                    operator=">",
                    value=15000,
                )

                rotator_symbols = scraper.filter_df(
                    dataframe=rotator_symbols,
                    filter_col="5m Spread",
                    operator=">",
                    value=0.15,
                )
                rotator_symbols.sort_values(by=["1m 1x Volume (USDT)", "Min qty"], inplace=True, ascending=[False, True])

                # Save rotator symbols
                scraper.output_df(
                    dataframe=rotator_symbols, 
                    path=f"/opt/bitnami/nginx/html/data/rotatorsymbols_{exchange_name}.json", 
                    to="json"
                )

                # If the exchange is bybit, save rotator symbols to the old path as well
                if exchange_name == "bybit":
                    scraper.output_df(dataframe=rotator_symbols, path="/opt/bitnami/nginx/html/data/rotatorsymbols.json", to="json")


                # Filter and save 'negative' funding data
                negative = scraper.filter_df(
                    dataframe=df, filter_col="Funding", operator="<", value=0
                )
                negative = scraper.reduce_df(
                    dataframe=negative,
                    columns=["Asset", "1m 1x Volume (USDT)", "Funding"],
                )
                scraper.output_df(
                    dataframe=negative, path=f"/opt/bitnami/nginx/html/data/negativefunding_{exchange_name}.csv", to="csv"
                )
                scraper.output_df(
                    dataframe=negative, path=f"/opt/bitnami/nginx/html/data/negativefunding_{exchange_name}.json", to="json"
                )

                # Filter and save 'positive' funding data
                positive = scraper.filter_df(
                    dataframe=df, filter_col="Funding", operator=">", value=0
                )
                positive = scraper.reduce_df(
                    dataframe=positive,
                    columns=["Asset", "1m 1x Volume (USDT)", "Funding"],
                )
                scraper.output_df(
                    dataframe=positive, path=f"/opt/bitnami/nginx/html/data/positivefunding_{exchange_name}.csv", to="csv"
                )
                scraper.output_df(
                    dataframe=positive, path=f"/opt/bitnami/nginx/html/data/positivefunding_{exchange_name}.json", to="json"
                )

                if exchange_name == "bybit":
                    total_historical_volume = scraper.get_all_historical_volume_bybit(interval="1h", limit=24)
                elif exchange_name == "binance":
                    total_historical_volume = scraper.get_all_historical_volume_binance(interval="1h", limit=24)
                    
                # Then save the JSON
                with open(f"/opt/bitnami/nginx/html/data/total_historical_volume_{exchange_name}.json", "w") as outfile:
                    json.dump(total_historical_volume, outfile)

                # # Get and save 'total_historical_volume' data
                # total_historical_volume = scraper.get_all_historical_volume(
                #     interval="1h", limit=24
                # )
                # with open(f"/opt/bitnami/nginx/html/data/total_historical_volume_{exchange_name}.json", "w") as outfile:
                #     json.dump(total_historical_volume, outfile)

        except pidfile.AlreadyRunningError:
            log.warning(f"{exchange_name} scraper already running.")
        except Exception as e:
            log.error(f"An unexpected error occurred for {exchange_name} scraper: {e}")
        finally:
            log.info(f"{exchange_name} scraper iteration completed. Waiting for the next run.")
            time.sleep(10)


if __name__ == "__main__":
    # Using threads to run both scrapers simultaneously
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        executor.submit(run_scraper_for_exchange, "binance")
        executor.submit(run_scraper_for_exchange, "bybit")
