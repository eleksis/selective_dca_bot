import argparse
import boto3
import configparser
import datetime
import time

from decimal import Decimal
from datetime import timedelta

from selective_dca_bot import config, utils
from selective_dca_bot.exchanges import BinanceExchange, ExchangesManager, EXCHANGE__BINANCE
from selective_dca_bot.models import Candle, LongPosition, MarketParams


parser = argparse.ArgumentParser(description='Selective DCA (Dollar Cost Averaging) Bot')

# Required positional arguments
parser.add_argument('buy_amount', type=Decimal,
                    help="The quantity of the crypto to spend (e.g. 0.05)")
parser.add_argument('base_pair',
                    help="""The ticker of the currency to spend (e.g. 'BTC',
                        'ETH', 'USD', etc)""")

# Optional switches
# parser.add_argument('-n', '--num_buys',
#                     default=1,
#                     dest="num_buys",
#                     type=int,
#                     help="Divide up the 'amount' across 'n' selective buys")

parser.add_argument('-e', '--exchanges',
                    default='binance',
                    dest="exchanges",
                    help="Comma-separated list of exchanges to include in this run")

parser.add_argument('-c', '--settings',
                    default="settings.conf",
                    dest="settings_config",
                    help="Override default settings config file location")

parser.add_argument('-p', '--portfolio',
                    default="portfolio.conf",
                    dest="portfolio_config",
                    help="Override default portfolio config file location")

parser.add_argument('-l', '--live',
                    action='store_true',
                    default=False,
                    dest="live_mode",
                    help="""Submit live orders. When omitted runs in simulation mode""")

parser.add_argument('-s', '--sells-enabled',
                    action='store_true',
                    default=False,
                    dest="sells_enabled",
                    help="""Sell off previously purchased positions if they've reached the profit threshold""")

parser.add_argument('-r', '--performance_report',
                    action='store_true',
                    default=False,
                    dest="performance_report",
                    help="""Compare purchase decisions against random portfolio selections""")


def get_timestamp():
    ts = time.time()
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


if __name__ == '__main__':
    args = parser.parse_args()
    buy_amount = args.buy_amount
    base_pair = args.base_pair
    live_mode = args.live_mode
    sells_enabled = args.sells_enabled
    config.is_test = not live_mode
    performance_report = args.performance_report
    # num_buys = args.num_buys
    exchange_list = args.exchanges.split(',')

    # Read settings
    arg_config = configparser.ConfigParser()
    arg_config.read(args.settings_config)

    binance_key = arg_config.get('API', 'BINANCE_KEY')
    binance_secret = arg_config.get('API', 'BINANCE_SECRET')

    max_crypto_holdings_percentage = Decimal(arg_config.get('CONFIG', 'MAX_CRYPTO_HOLDINGS_PERCENTAGE'))
    ma_ratio_profit_threshold = Decimal(arg_config.get('CONFIG', 'MA_RATIO_PROFIT_THRESHOLD'))
    min_profit = Decimal(arg_config.get('CONFIG', 'MIN_PROFIT'))
    profit_threshold = Decimal(arg_config.get('CONFIG', 'PROFIT_THRESHOLD'))

    try:
        sns_topic = arg_config.get('AWS', 'SNS_TOPIC')
        aws_access_key_id = arg_config.get('AWS', 'AWS_ACCESS_KEY_ID')
        aws_secret_access_key = arg_config.get('AWS', 'AWS_SECRET_ACCESS_KEY')

        # Prep boto SNS client for email notifications
        sns = boto3.client(
            "sns",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name="us-east-1"     # N. Virginia
        )
    except configparser.NoSectionError:
        sns_topic = None

    if performance_report:
        from selective_dca_bot.models import LongPosition, Candle
        results = LongPosition.get_performance_report()
        exit()

    # Read crypto watchlist
    arg_config = configparser.ConfigParser()
    arg_config.read(args.portfolio_config)

    watchlist = []
    binance_watchlist = [x.strip() for x in arg_config.get('WATCHLIST', 'BINANCE').split(',')]
    watchlist.extend(binance_watchlist)

    params = {
        # "num_buys": num_buys,
    }

    # Setup package-wide settings
    config.params = params
    config.interval = Candle.INTERVAL__1HOUR

    # If multiple MA periods are passed, will calculate the price-to-MA with the lowest MA
    ma_periods = [200]


    #------------------------------------------------------------------------------------
    # UPDATE latest candles
    over_positioned = []
    num_positions = {}
    total_positions = LongPosition.get_num_positions(limit=max(ma_periods))

    # Are we too heavily weighted on a crypto on our watchlist over the last N periods?
    for crypto in watchlist:
        market = f"{crypto}{base_pair}"
        num_positions[crypto] = LongPosition.get_num_positions(market, limit=max(ma_periods))
        if Decimal(num_positions[crypto] / total_positions) >= max_crypto_holdings_percentage:
            over_positioned.append(crypto)

    exchanges_data = []
    if EXCHANGE__BINANCE in exchange_list:
        exchanges_data.append(
            {
                'name': EXCHANGE__BINANCE,
                'key': binance_key,
                'secret': binance_secret,
                'watchlist': binance_watchlist,
            }
        )

    exchanges = ExchangesManager.get_exchanges(exchanges_data)
    metrics = []
    for name, exchange in exchanges.items():
        metrics.extend(exchange.calculate_latest_metrics(base_pair=base_pair, interval=config.interval, ma_periods=ma_periods))


    #------------------------------------------------------------------------------------
    # SELL any LongPositions?
    if sells_enabled:
        for exchange_name, exchange in exchanges.items():
            for crypto in AllTimeWatchlist.get_watchlist(exchange=exchange_name):
                market = f"{crypto}{base_pair}"
                current_price = Candle.get_last_candle(market, config.interval).close
                current_ma_ratio = metrics[market]['price_to_ma']
                positions_to_sell = []
                sell_quantity = Decimal('0.0')
                total_spent = Decimal('0.0')
                for position in LongPosition.get_open_positions(market):
                    # SELL if:
                    #   - position is at or above profit_threshold
                    #   - OR if the current_ma_ratio is spiking and the position is at or
                    #       above the min_profit level
                    if (current_price / position.purchase_price >= profit_threshold or
                            (current_ma_ratio >= ma_ratio_profit_threshold and
                             current_price / position.purchase_price >= min_profit)):
                        positions_to_sell.append(position)
                        sell_quantity += position.buy_quantity
                        total_spent += position.spent

                if sell_quantity > Decimal('0.0'):
                    print(f"SELL {sell_quantity.normalize()} {crypto}")

                    # Check balance and make sure we can actually sell this much
                    balance = exchange.get_current_balance(crypto)
                    if sell_quantity > balance:
                        subject = f"Insufficient balance"
                        message = f"Can't sell {sell_quantity.normalize()}. Only {balance.normalize()} {crypto} held on {exchange.exchange_name}"
                        print(message)
                        sns.publish(
                            TopicArn=sns_topic,
                            Subject=subject,
                            Message=message
                        )
                        continue

                    if live_mode:
                        result = exchange.market_sell(market, sell_quantity)
                        print(result)
                        """
                            result = {
                                "order_id": order_id,
                                "price": sell_price,
                                "quantity": total_qty,
                                "fees": total_commission,
                                "timestamp": timestamp
                            }
                        """
                        for position in positions_to_sell:
                            position.sell_quantity = position.buy_quantity
                            position.sell_price = result['price']
                            position.sell_timestamp = result['timestamp']
                            position.save()

                        profit = (sell_quantity * result['price']) - total_spent
                        profit_percentage = ((sell_quantity * result['price']) / total_spent * Decimal('100.0')).quantize(Decimal('0.01'))

                        # Send SNS message
                        subject = f"SOLD {sell_quantity.normalize()} {crypto} for {profit:0.8f}{base_pair} ({profit_percentage}%)"
                        print(subject)
                        message = f"num_positions: {len(positions_to_sell)}\n"
                        for position in positions_to_sell:
                            profit = (position.buy_quantity * result['price']) - position.spent
                            profit_percentage = ((position.buy_quantity * result['price']) / position.spent * Decimal('100.0')).quantize(Decimal('0.01'))
                            message += f"{position.timestamp}: {profit:0.8f} | {profit_percentage:3.2f}%\n"

                        sns.publish(
                            TopicArn=sns_topic,
                            Subject=subject,
                            Message=message
                        )


    #------------------------------------------------------------------------------------
    #  BUY the next target based on the most favorable price_to_ma ratio
    metrics_sorted = sorted(metrics, key = lambda i: i['price_to_ma'])
    ma_ratios = ""
    target_metric = None
    for metric in metrics_sorted:
        crypto = metric['market'][:(-1 * len(base_pair))]
        if crypto not in watchlist:
            # This is a historical crypto being updated
            continue

        ma_ratios += f"{crypto}: price-to-MA: {metric['price_to_ma']:0.4f} | positions: {num_positions[crypto]}\n"

        # Our target crypto's metric will be the first one on this list that isn't overpositioned
        if not target_metric and crypto not in over_positioned:
            target_metric = metric
    print(ma_ratios)

    if not target_metric:
        # All of the cryptos failed the over positioned test?!
        target_metric = metrics_sorted[0]

    # Set up a market buy for the first result that isn't overpositioned
    market = target_metric['market']
    crypto = market[:(-1 * len(base_pair))]
    exchange_name = target_metric['exchange']
    exchange = exchanges[exchange_name]
    ma_period = target_metric['ma_period']
    price_to_ma = target_metric['price_to_ma']

    current_price = exchange.get_current_ask(market)

    quantity = buy_amount / current_price

    market_params = MarketParams.get_market(market)
    quantized_qty = quantity.quantize(market_params.lot_step_size)

    if quantized_qty * current_price < market_params.min_notional:
        # Final order size isn't big enough
        print(f"Must increase quantized_qty: {quantized_qty} * {current_price} < {market_params.min_notional}")
        quantized_qty += market_params.lot_step_size

    print(f"Buy: {quantized_qty:0.6f} {crypto} @ {current_price:0.8f} {base_pair}")

    if live_mode:
        results = exchange.buy(market, quantized_qty)

        LongPosition.create(
            market=market,
            buy_order_id=results['order_id'],
            buy_quantity=results['quantity'],
            purchase_price=results['price'],
            fees=results['fees'],
            timestamp=results['timestamp'],
            watchlist=",".join(watchlist),
        )

        current_profit = utils.current_profit()
        print(current_profit)

        # Send SNS message
        subject = f"Bought {results['quantity'].normalize()} {crypto} ({price_to_ma*Decimal('100.0'):0.2f}% of {ma_period}-hr MA)"
        print(subject)
        message = ma_ratios
        message += "\n\n" + current_profit

        sns.publish(
            TopicArn=sns_topic,
            Subject=subject,
            Message=message
        )

