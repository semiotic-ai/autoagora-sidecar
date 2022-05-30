# Copyright 2022-, Semiotic AI, Inc.
# SPDX-License-Identifier: Apache-2.0

import asyncio as aio
import logging
import signal

import aio_pika
import configargparse
from anyio import (
    DelimiterNotFound,
    create_memory_object_stream,
    create_task_group,
    create_udp_socket,
    open_signal_receiver,
)
from anyio.abc import CancelScope
from anyio.streams.buffered import BufferedByteReceiveStream
from ratelimitingfilter import RateLimitingFilter

argsparser = configargparse.ArgParser()
argsparser.add_argument(
    "--logs-receive-port",
    env_var="LOGS_RECEIVE_PORT",
    type=int,
    default=31338,
    required=False,
    help="UDP port to receive the GQL logs from the query-node on.",
)
argsparser.add_argument(
    "--rabbitmq-host",
    env_var="RABBITMQ_HOST",
    required=True,
    help="Hostname of the RabbitMQ server used for queuing the GQL logs.",
)
argsparser.add_argument(
    "--rabbitmq-queue-name",
    env_var="RABBITMQ_QUEUE_NAME",
    default="gql_logs_processor",
    required=False,
    help="Name of the RabbitMQ queue query-node logs are pushed to for processing.",
)
argsparser.add_argument(
    "--rabbitmq-exchange-name",
    env_var="RABBITMQ_EXCHANGE_NAME",
    default="gql_logs",
    required=False,
    help="Name of the RabbitMQ exchange query-node logs are pushed to.",
)
argsparser.add_argument(
    "--rabbitmq-queue-limit",
    env_var="RABBITMQ_QUEUE_LIMIT",
    type=int,
    default=1000,
    required=False,
    help="Size limit of the created RabbitMQ queue. It is discouraged to change that "
    "value while the system is running, because it requires manual destruction of the "
    "queue and a restart of the whole Auto Agora stack.",
)
argsparser.add_argument(
    "--rabbitmq-username",
    env_var="RABBITMQ_USERNAME",
    type=str,
    default="guest",
    required=False,
    help="Username to use for the GQL logs RabbitMQ queue.",
)
argsparser.add_argument(
    "--rabbitmq-password",
    env_var="RABBITMQ_PASSWORD",
    type=str,
    default="guest",
    required=False,
    help="Password to use for the GQL logs RabbitMQ queue.",
)
argsparser.add_argument(
    "--max-cache-lines",
    env_var="MAX_CACHE_LINES",
    type=int,
    default=100,
    required=False,
    help="Maximum number of log lines to cache locally.",
)
argsparser.add_argument(
    "--log-level",
    env_var="LOG_LEVEL",
    type=str,
    choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    default="WARNING",
    required=False,
)
args = argsparser.parse_args()

logging.root.setLevel(args.log_level)
logging_stream_handler = logging.StreamHandler()
logging_stream_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
)
logging_stream_handler.setLevel(args.log_level)
logging.root.addHandler(logging_stream_handler)
logging_stream_handler.addFilter(
    RateLimitingFilter(
        rate=1,
        per=2,
        burst=3,
        match=[
            "Local cache full",
            "exception while pushing log entry to RabbitMQ instance",
        ],
    )
)

LINE_LIMIT = 1048576
send_udp_buffer, receive_udp_buffer = create_memory_object_stream()
q = aio.Queue(maxsize=args.max_cache_lines)
terminate = False
loop = aio.get_event_loop()


async def udp_echo_server():
    async with await create_udp_socket(
        local_host="0.0.0.0", local_port=args.logs_receive_port
    ) as udp:
        async for packet, _ in udp:
            logging.debug("Got UDP data: %s", packet.decode())
            await send_udp_buffer.send(packet)

    logging.info("UDP echo server loop finished.")


async def readlines():
    global terminate
    buffered = BufferedByteReceiveStream(receive_udp_buffer)
    while not terminate:
        try:
            line = await buffered.receive_until(b"\n", LINE_LIMIT)
        except DelimiterNotFound:
            logging.exception("%s", (await buffered.receive_exactly(8192)).decode())
            continue

        logging.debug("Got logs line: %s", line)

        try:
            q.put_nowait(line)
        except aio.QueueFull:
            logging.error("Local cache full, dropping log line to relieve pressure.")

    logging.info("Readlines loop finished.")


async def push_rabbit():
    global terminate

    connection = await aio_pika.connect_robust(
        f"amqp://{args.rabbitmq_username}:{args.rabbitmq_password}"
        f"@{args.rabbitmq_host}/",
        loop=loop,
    )
    async with connection:
        channel = await connection.channel()

        exchange = await channel.declare_exchange(
            name=args.rabbitmq_exchange_name, type=aio_pika.ExchangeType.FANOUT
        )

        queue = await channel.declare_queue(
            name=args.rabbitmq_queue_name,
            arguments={
                "x-max-length": args.rabbitmq_queue_limit,
                "x-overflow": "drop-head",
            },
        )

        await queue.bind(exchange)

        while not (terminate and q.empty()):
            line = await q.get()
            try:
                await exchange.publish(aio_pika.Message(body=line), routing_key="")
            except Exception as e:
                logging.error(
                    '"%s" exception while pushing log entry to RabbitMQ instance. '
                    "Log entry is dropped.",
                    str(e),
                )
            q.task_done()

    await connection.close()

    logging.info("Rabbit push loop finished.")


async def signal_handler(scope: CancelScope):
    global terminate

    with open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for signum in signals:
            if signum == signal.SIGINT:
                logging.info("Received SIGINT")
            else:
                logging.info("Received SIGTERM")

            terminate = True

            scope.cancel()
            return


async def async_main():
    async with create_task_group() as tg:
        tg.start_soon(udp_echo_server)
        tg.start_soon(readlines)
        tg.start_soon(push_rabbit)
        tg.start_soon(signal_handler, tg.cancel_scope)


def main():
    logging.info("Hello.")

    loop.run_until_complete(async_main())

    logging.info("Goodbye.")


if __name__ == "__main__":
    main()
