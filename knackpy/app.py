import csv
import datetime
import logging
import os
import warnings
import typing

import requests
import pytz

from . import api, fields, records, utils
from .models import TIMEZONES


class App(object):
    """Knackpy is designed around the `App` class. It provides helpers for querying
    and manipulating Knack application data. You should use the `App` class
    because:

    - It allows you to query obejcts and views by key or name
    - It takes care of [localization issues](#timestamps-and-localization)
    - It let's you download and upload files from your app.
    - It does other things, too.

    Args:
        app_id (str): Knack [application ID](https://www.knack.com/developer-documentation/#find-your-api-key-amp-application-id)  # noqa:E501
            string.
        api_key (str, optional, default=`None`): [Knack API key](https://www.knack.com/developer-documentation/#find-your-api-key-amp-application-id).
        metadata (dict, optional): The Knack app's metadata as a `dict`. If `None`
            it will be fetched on init. You can find your apps metadata
            [here](https://loader.knack.com/v1/applications/5d79512148c4af00106d1507).
        tzinfo (`pytz.Timezone`, optional): [description].  A
            [pytz.Timezone](https://pythonhosted.org/pytz/) object. When `None`, is set
            automatically based on the app's `metadadata`.
        max_attempts (int): The maximum number of attempts to make if a request times
            out. Default values that are set in `knackpy.api.request`.
        timeout (int, optional): Number of seconds to wait before a Knack API request
            times out. Further reading:
            [Requests docs](https://requests.readthedocs.io/en/master/user/quickstart/).
    """

    def __repr__(self):
        return f"""<App [{self.metadata["name"]}]>"""

    def __init__(
        self,
        *,
        app_id: str,
        api_key: str = None,
        metadata: str = None,
        tzinfo: datetime.tzinfo = None,
        max_attempts: int = None,
        timeout: int = None,
    ):

        if not api_key:
            warnings.warn(
                "No API key has been supplied. Only public views will be accessible."
            )

        self.app_id = app_id
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.metadata = (
            self._get_metadata()["application"]
            if not metadata
            else metadata["application"]
        )
        self.tzinfo = tzinfo if tzinfo else self.metadata["settings"]["timezone"]
        self.timezone = self._get_timezone(self.tzinfo)
        self.field_defs = fields.field_defs_from_metadata(self.metadata)
        self.containers = utils.generate_containers(self.metadata)
        self.data = {}
        logging.debug(self)

    def _get_metadata(self):
        return api.get_metadata(app_id=self.app_id, timeout=self.timeout)

    def info(self):
        total_obj = len(self.metadata.get("objects"))
        total_scenes = len(self.metadata.get("scenes"))
        total_records = self.metadata.get("counts").get("total_entries")
        total_size = utils.humanize_bytes(self.metadata.get("counts").get("asset_size"))

        return {
            "objects": total_obj,
            "scenes": total_scenes,
            "records": total_records,
            "size": total_size,
        }

    @staticmethod
    def _get_timezone(tzinfo: str):
        # TODO: move to utils
        """Create a pytz.Timezone instance from a timezone string.

        Knack stores timezone information in the app metadata, but it does not
        use IANA timezone database names. Instead it uses common names e.g.,
        "Eastern Time (US & Canada)" instead of "US/Eastern".

        I'm sure these common names are standardized somewhere, and I did not
        bother to munge the IANA timezone DB to figure it out, so I created the
        `TIMZONES` index in `knackpy.utils.timezones` by copying a table from
        the internets.

        As such, we can't be certain our index contains all of the timezone
        names that knack uses in its metadata. So, this method will attempt to
        lookup the Knack metadata timezone in our TIMEZONES index, and raise an
        error of it fails.

        Alternatively, the client can override the Knack timezone common name by
        including an IANA-compliant timezone name (e.g., "US/Central")by passing
        the `tzinfo` kwarg when constructing the `App` innstance, or directly to
        this method.

        See also, note in knackpy.fields.real_unix_timestamp_mills() about why
        we need valid timezone info to handle Knack records.

        Inputs:
        - tzinfo (str): either an IANA-compliant timezone name, or the common
          timezone name available in metadata.settings.timezone

        Returns (hopefully): - a `pytz.timezone` instance
        """

        try:
            # first let pytz try to handle the tzinfo
            return pytz.timezone(tzinfo)
        except pytz.exceptions.UnknownTimeZoneError:
            pass

        try:
            # perhaps the tzinfo matches a known timezone common name
            matches = [
                tz["iana_name"]
                for tz in TIMEZONES
                if tz["common_name"].lower() == tzinfo.lower()
            ]
            return pytz.timezone(matches[0])

        except (pytz.exceptions.UnknownTimeZoneError, IndexError):
            pass

        raise ValueError(
            """
                Unknown timezone supplied. `tzinfo` should formatted as a
                timezone string compliant to the IANA timezone database. See:
                https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
            """
        )

    def _find_container(self, identifier: str):
        matches = [
            container
            for container in self.containers
            if identifier in [container.obj, container.view, container.name]
        ]

        if len(matches) > 1:
            raise ValueError(
                f"Multiple containers use name {identifier}. Try using a view or object key."  # noqa
            )

        try:
            return matches[0]
        except IndexError:
            raise IndexError(
                f"Unknown container specified: {identifier}. Inspect App.containers for available containers."  # noqa
            )

    def _build_request_kwargs(self, **kwargs) -> dict:
        """Compile the keyword arguments to be passed to `knackpy.api`. We drop params
        that are NoneType because we don't want to override the default values for
        these params that are define in the api methods.

        Args:
            record_limit (int): the maximum number of records to retrieve.
            max_attempts (int): The maximum number of attempts to make if a request
                times out.
            timeout (int, optional): Number of seconds to wait before a Knack API
                request times out. Further reading:
                [Requests docs](https://requests.readthedocs.io/en/master/user/quickstart/).  # noqa:E501
        """
        supported_kwargs = ["record_limit", "max_attempts", "timeout"]

        return {key: kwargs[key] for key in supported_kwargs if kwargs.get(key)}

    def records(
        self,
        identifier: str = None,
        refresh: bool = False,
        record_limit: int = None,
        filters: typing.Union[dict, list] = None,
    ):
        """Get records from a knack object or view. Supported kwargs are record_limit
            (type: int), max_attempts (type: int), and filters (type: dict).

            Note that we accept the request params `record_limit` and `filters` here
            because the user would presumably want to set these on a per-object/view
            basis. They are not stored in state. Whereas `max_attempts` and
            `timtout` are set on App construction and persist in `App` state.

            Args:
                identifier (str, optional*): an object or view key or name string that
                    exists in the app. If None is provided and only one container has
                    been fetched, will return records from that container.
                refresh (bool, optional): Force the re-querying of data from Knack
                    API. Defaults to False.
                record_limit (int): the maximum number of records to retrieve.
                    Default value is set in `knackpy.api.request`.
                filters (dict or list, optional): A dict or of Knack API filiters.
                    See: https://www.knack.com/developer-documentation/#filters.

            Returns:
                [generator]: A generator which yields Knack record data.
        """
        if not identifier and len(self.data) == 1:
            identifier = list(self.data.keys())[0]
        elif not identifier:
            raise TypeError("Missing 1 required argument: identifier")

        container = self._find_container(identifier)

        container_key = container.obj or container.view

        if not self.data.get(container_key) or refresh:
            request_kwargs = self._build_request_kwargs(
                max_attempts=self.max_attempts,
                timeout=self.timeout,
                record_limit=record_limit,
            )

            self.data[container_key] = api.get(
                app_id=self.app_id,
                api_key=self.api_key,
                obj=container.obj,
                scene=container.scene,
                view=container.view,
                filters=filters,
                **request_kwargs,
            )

        return self._generate_records(container_key, self.data[container_key])

    def _generate_records(self, container_key, data):
        return records.Records(
            container_key, data, self.field_defs, self.timezone
        ).records()

    def _find_field_def(self, identifier, obj):
        return [
            field_def
            for field_def in self.field_defs
            if identifier.lower() in [field_def.name.lower(), field_def.key]
            and field_def.obj == obj
        ]

    def to_csv(self, identifier: str, *, out_dir: str = "_csv", delimiter=",") -> None:
        """Write formatted Knack records to CSV.

        Args:
            identifier (str): an object or view key or name string that exists in the
                app.
            out_dir (str, optional): Relative path to the directory to which files
                will be written. Defaults to "_csv".
            delimiter (str, optional): [description]. Defaults to ",".
        """
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        records = self.records(identifier)

        csv_data = [record.format() for record in records]

        fieldnames = csv_data[0].keys()

        fname = os.path.join(out_dir, f"{identifier}.csv")

        with open(fname, "w") as fout:
            writer = csv.DictWriter(fout, fieldnames=fieldnames, delimiter=delimiter)
            writer.writeheader()
            writer.writerows(csv_data)

    def _assemble_downloads(
        self, identifier: str, field_key: str, label_keys: list, out_dir: str
    ):
        """Extract file download paths and custom filenames/output paths.

        Args:
            identifier (str): The name or key of the object from which files will be
                downloaded.
           field_key (str): The knack field key to be downloaded (must be a "file" or
            "image" field type)
            label_keys (list, optional): A list of field keys whose *values* will be
                prepended to the attachment filename, separated by an underscore.
            out_dir (str, optional): Relative path to the directory to which files
                will be written. Defaults to "_downloads".

        Returns:
            list: A list of dictionaries with file properties that will be passed to
                the HTTP request. Dict's look like this:
            {
                "id": "5d7967132be2bb0010892ce7",
                "application_id": "abc123xzy456",
                "s3": true,
                "type": "file",
                "filename": "_data/my_file.pdf",
                "url": "https://api.knack.com/v1/applications/abc123xzy456/download/asset/5d7967132be2bb0010892ce7/my_file.pdf",   # noqa:E501
                "thumb_url": "",
                "size": 305741,
                "field_key": "field_17"
            }
        """
        # TODO: support
        downloads = []

        field_key_raw = f"{field_key}_raw"

        downloads = []

        for record in self.records(identifier):
            file_dict = record.raw.get(field_key_raw)

            if not file_dict:
                continue

            filename = file_dict["filename"]

            if label_keys:
                # reverse traverse to ensure that field labels are prepended in
                # sequence provided.
                for field in reversed(label_keys):
                    filename = f"{record.raw.get(field)}_{filename}"

            file_dict["filename"] = os.path.join(out_dir, filename)

            downloads.append(file_dict)
        breakpoint()
        return downloads

    def _download_files(self, downloads: list):
        """Download files from Knack and write them locally.

        Args:
            downloads (list): A list of dictionaries with file properties that will be
            passed to the HTTP request. Dict's look like this:

            {
                "id": "5d7967132be2bb0010892ce7",
                "application_id": "abc123xzy456",
                "s3": true,
                "type": "file",
                "filename": "_data/my_file.pdf",
                "url": "https://api.knack.com/v1/applications/abc123xzy456/download/asset/5d7967132be2bb0010892ce7/my_file.pdf",  # noqa:E501
                "thumb_url": "",
                "size": 305741,
                "field_key": "field_17"
            }

        Returns:
            int: A count of the number of files downloaded.

        """
        count = 0

        for file_info in downloads:
            filename = file_info["filename"]
            filesize = utils.humanize_bytes(file_info["size"])
            logging.debug(f"\nDownloading {file_info['url']} - size: {filesize}")

            res = requests.get(file_info["url"], allow_redirects=True)

            res.raise_for_status()

            with open(filename, "wb") as fout:
                fout.write(res.content)
                count += 1

        return count

    def download(
        self,
        identifier: str,
        *,
        field: str,
        out_dir: str = "_downloads",
        label_keys: list = None,
    ):
        """Download files and images from Knack records.

        Args:
            identifier (str): The name or key of the object from which files will be
                downloaded.
            out_dir (str, optional): Relative path to the directory to which files
                will be written. Defaults to "_downloads".
            field (str): The Knack field key of the file or image field to be
                downloaded.
            label_keys (list, optional): A list of field keys whose *values* will be
                prepended to the attachment filename, separated by an underscore.

        Returns:
            [int]: Count of files downloaded.
        """
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)

        container = self._find_container(identifier)

        field_defs = self._find_field_def(field, identifier)

        if not field_defs:
            raise ValueError(f"Field not found: '{field}'")

        downloads = self._assemble_downloads(
            container.obj, field_defs[0].key, label_keys, out_dir
        )

        download_count = self._download_files(downloads)

        logging.debug(f"{download_count} files downloaded.")

        return download_count
