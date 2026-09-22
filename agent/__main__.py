"""
The agent's entry point.

One deployable, three modules, one command: ``python -m agent <module>``. Each module runs on its own
trigger - dataset sync after a local extraction, the publisher after a steward approves, the
monitoring import at retraining - so the container runs one of them per invocation rather than a
daemon that does all three.
"""

import argparse
import logging
import sys
from typing import List, Optional

from . import config as config_module
from .auth import AuthenticationError, ServiceAccountTokenProvider
from .config import ConfigError
from .modules import dataset_sync, metadata_publisher, monitoring_import
from .passport import PassportClient, PassportError

logger = logging.getLogger("agent")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="AI4HF Passport Node Agent - the node side of the federated passport.",
    )
    parser.add_argument(
        "module",
        choices=list(config_module.MODULES),
        help="which module to run",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log every request the agent makes",
    )

    retraining = parser.add_argument_group("monitoring-import")
    retraining.add_argument(
        "--model-id",
        help="the model being replaced; its deployment period is what gets imported",
    )
    retraining.add_argument("--period-start", help="ISO-8601 start of the deployment period")
    retraining.add_argument("--period-end", help="ISO-8601 end of the deployment period")
    retraining.add_argument("--reason", help="what prompted the retraining")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """
    Run one module.

    :param argv: command line arguments, defaulting to sys.argv
    :return: a process exit code
    """
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        settings = config_module.load(args.module)
        tokens = ServiceAccountTokenProvider(settings.keycloak, settings.request_timeout)
        passport = PassportClient(
            settings.passport_url, settings.study_id, tokens, settings.request_timeout
        )

        logger.info(
            "Running %s for organization %s in study %s as %s",
            settings.module,
            settings.organization_id,
            settings.study_id,
            tokens.client_id,
        )

        if args.module == "dataset-sync":
            config_module.require_for_dataset_sync(settings)
            summary = dataset_sync.run(settings, passport)

        elif args.module == "metadata-publisher":
            config_module.require_for_metadata_publisher(settings)
            summary = metadata_publisher.run(settings, passport)

        else:
            config_module.require_for_monitoring_import(settings)
            if not args.model_id:
                raise ConfigError(
                    "monitoring-import needs --model-id: the model being replaced. "
                    "There is no retraining-event contract yet, so the run is triggered explicitly."
                )
            summary = monitoring_import.run(
                settings,
                passport,
                model_id=args.model_id,
                period_start=args.period_start,
                period_end=args.period_end,
                reason=args.reason,
            )

        logger.info("Done: %s", summary)
        return 0

    except ConfigError as error:
        logger.error("Configuration: %s", error)
        return 2
    except AuthenticationError as error:
        logger.error("Authentication: %s", error)
        return 3
    except PassportError as error:
        logger.error("Passport: %s", error)
        return 4
    except Exception as error:  # noqa: BLE001 - the container needs a non-zero exit, not a traceback
        logger.error("%s: %s", type(error).__name__, error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
