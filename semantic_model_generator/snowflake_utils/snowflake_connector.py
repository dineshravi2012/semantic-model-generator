import concurrent.futures
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional, TypeVar

import pandas as pd
from loguru import logger
from snowflake.connector import DictCursor
from snowflake.connector.connection import SnowflakeConnection
from snowflake.connector.errors import ProgrammingError

from semantic_model_generator.data_processing.data_types import Column, Table
from semantic_model_generator.snowflake_utils import env_vars
from semantic_model_generator.snowflake_utils.utils import snowflake_connection

ConnectionType = TypeVar("ConnectionType")
# This is the raw column name from snowflake information schema or desc table
_COMMENT_COL = "COMMENT"
_COLUMN_NAME_COL = "COLUMN_NAME"
_DATATYPE_COL = "DATA_TYPE"
_TABLE_SCHEMA_COL = "TABLE_SCHEMA"
_TABLE_NAME_COL = "TABLE_NAME"
# Below are the renamed column names when we fetch into dataframe, to differentiate between table/column comments
_COLUMN_COMMENT_ALIAS = "COLUMN_COMMENT"
_TABLE_COMMENT_COL = "TABLE_COMMENT"

# https://docs.snowflake.com/en/sql-reference/data-types-datetime
TIME_MEASURE_DATATYPES = [
    "DATE",
    "DATETIME",
    "TIMESTAMP_LTZ",
    "TIMESTAMP_NTZ",
    "TIMESTAMP_TZ",
    "TIMESTAMP",
    "TIME",
]
# https://docs.snowflake.com/en/sql-reference/data-types-text
DIMENSION_DATATYPES = [
    "VARCHAR",
    "CHAR",
    "CHARACTER",
    "NCHAR",
    "STRING",
    "TEXT",
    "NVARCHAR",
    "NVARCHAR2",
    "CHAR VARYING",
    "NCHAR VARYING",
    "BINARY",
    "VARBINARY",
]
# https://docs.snowflake.com/en/sql-reference/data-types-numeric
MEASURE_DATATYPES = [
    "NUMBER",
    "DECIMAL",
    "DEC",
    "NUMERIC",
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "BYTEINT",
    "FLOAT",
    "FLOAT4",
    "FLOAT8",
    "DOUBLE",
    "DOUBLE PRECISION",
    "REAL",
]
OBJECT_DATATYPES = ["VARIANT", "ARRAY", "OBJECT", "GEOGRAPHY"]


_QUERY_TAG = "SEMANTIC_MODEL_GENERATOR"


def get_table_representation(
    conn: SnowflakeConnection,
    schema_name: str,
    table_name: str,
    table_index: int,
    ndv_per_column: int,
    columns_df: pd.DataFrame,
    max_workers: int,
) -> Table:
    table_comment = columns_df[_TABLE_COMMENT_COL].iloc[0]

    def _get_col(col_index: int, column_row: pd.Series) -> Column:
        return _get_column_representation(
            conn=conn,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_row[_COLUMN_NAME_COL],
            column_comment=column_row[_COLUMN_COMMENT_ALIAS],
            column_index=col_index,
            column_datatype=column_row[_DATATYPE_COL],
            ndv=ndv_per_column,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_col_index = {
            executor.submit(_get_col, col_index, column_row): col_index
            for col_index, (_, column_row) in enumerate(columns_df.iterrows())
        }
        index_and_column = []
        for future in concurrent.futures.as_completed(future_to_col_index):
            col_index = future_to_col_index[future]
            column = future.result()
            index_and_column.append((col_index, column))
        columns = [c for _, c in sorted(index_and_column, key=lambda x: x[0])]

    return Table(
        id_=table_index, name=table_name, comment=table_comment, columns=columns
    )


def _get_column_representation(
    conn: SnowflakeConnection,
    schema_name: str,
    table_name: str,
    column_name: str,
    column_comment: str,
    column_index: int,
    column_datatype: str,
    ndv: int,
) -> Column:
    column_values = None
    if ndv > 0:
        # Pull sample values.
        try:
            cursor = conn.cursor(DictCursor)
            assert cursor is not None, "Cursor is unexpectedly None"
            cursor_execute = cursor.execute(
                f'select distinct "{column_name}" from "{schema_name}"."{table_name}" limit {ndv}'
            )
            assert cursor_execute is not None, "cursor_execute should not be none "
            res = cursor_execute.fetchall()
            # Cast all values to string to ensure the list is json serializable.
            # A better solution would be to identify the possible types that are not
            # json serializable (e.g. datetime objects) and apply the appropriate casting
            # in just those cases.
            if len(res) > 0:
                if isinstance(res[0], dict):
                    col_key = [k for k in res[0].keys()][0]
                    column_values = [str(r[col_key]) for r in res]
                else:
                    raise ValueError(
                        f"Expected the first item of res to be a dict. Instead passed {res}"
                    )
        except Exception as e:
            logger.error(f"unable to get values: {e}")

    column = Column(
        id_=column_index,
        column_name=column_name,
        comment=column_comment,
        column_type=column_datatype,
        values=column_values,
    )
    return column


def _fetch_valid_tables_and_views(conn: SnowflakeConnection) -> pd.DataFrame:
    def _get_df(query: str) -> pd.DataFrame:
        cursor = conn.cursor().execute(query)
        assert cursor is not None, "cursor should not be none here."

        df = pd.DataFrame(
            cursor.fetchall(), columns=[c.name for c in cursor.description]
        )
        return df[["name", "schema_name", "comment"]].rename(
            columns=dict(
                name=_TABLE_NAME_COL,
                schema_name=_TABLE_SCHEMA_COL,
                comment=_TABLE_COMMENT_COL,
            )
        )

    tables = _get_df("show tables in database")
    views = _get_df("show views in database")
    return pd.concat([tables, views], axis=0)


def get_valid_schemas_tables_columns_df(
    conn: SnowflakeConnection,
    table_schema: Optional[str] = None,
    table_names: Optional[List[str]] = None,
) -> pd.DataFrame:
    if table_names and not table_schema:
        logger.warning(
            "Provided table_name without table_schema, cannot filter to fetch the specific table"
        )

    where_clause = ""
    if table_schema:
        where_clause += f" where t.table_schema ilike '{table_schema}' "
        if table_names:
            table_names_str = ", ".join([f"'{t.lower()}'" for t in table_names])
            where_clause += f"AND LOWER(t.table_name) in ({table_names_str}) "
    query = f"""select t.{_TABLE_SCHEMA_COL}, t.{_TABLE_NAME_COL}, c.{_COLUMN_NAME_COL}, c.{_DATATYPE_COL}, c.{_COMMENT_COL} as {_COLUMN_COMMENT_ALIAS}
from information_schema.tables as t
join information_schema.columns as c on t.table_schema = c.table_schema and t.table_name = c.table_name{where_clause}
order by 1, 2, c.ordinal_position"""
    cursor_execute = conn.cursor().execute(query)
    assert cursor_execute, "cursor_execute should not be None here"
    schemas_tables_columns_df = cursor_execute.fetch_pandas_all()

    valid_tables_and_views_df = _fetch_valid_tables_and_views(conn=conn)

    valid_schemas_tables_columns_df = valid_tables_and_views_df.merge(
        schemas_tables_columns_df, how="inner", on=(_TABLE_SCHEMA_COL, _TABLE_NAME_COL)
    )
    return valid_schemas_tables_columns_df


class SnowflakeConnector:
    def __init__(
        self,
        account_name: str,
        max_workers: int = 1,
    ):
        self.account_name: str = account_name
        self._max_workers = max_workers

    # Required env vars below
    def _get_role(self) -> str:
        role = env_vars.SNOWFLAKE_ROLE
        if not role:
            raise ValueError(
                "You need to set an env var for the snowflake role. export SNOWFLAKE_ROLE=<your-snowflake-role>"
            )
        return role

    def _get_user(self) -> str:
        user = env_vars.SNOWFLAKE_USER
        if not user:
            raise ValueError(
                "You need to set an env var for the snowflake user. export SNOWFLAKE_USER=<your-snowflake-user>"
            )
        return user

    def _get_password(self) -> str:
        password = env_vars.SNOWFLAKE_PASSWORD
        if not password:
            raise ValueError(
                "You need to set an env var for the snowflake password. export SNOWFLAKE_PASSWORD=<your-snowflake-password>"
            )
        return password

    def _get_warehouse(self) -> str:
        warehouse = env_vars.SNOWFLAKE_WAREHOUSE
        if not warehouse:
            raise ValueError(
                "You need to set an env var for the snowflake warehouse. export SNOWFLAKE_WAREHOUSE=<your-snowflake-warehouse-name>"
            )
        return warehouse

    def _get_host(self) -> Optional[str]:
        host = env_vars.SNOWFLAKE_HOST
        if not host:
            logger.info(
                "No host set. Attempting to connect without. To set export SNOWFLAKE_HOST=<snowflake-host-name>"
            )
        return host

    @contextmanager
    def connect(
        self, db_name: str, schema_name: Optional[str] = None
    ) -> Generator[SnowflakeConnection, None, None]:
        """Opens a connection to the database and optional schema.

        This function is a context manager for a connection that can be used to execute queries.
        Example usage:

        with connector.connect(db_name="my_db", schema_name="my_schema") as conn:
            connector.execute(conn=conn, query="select * from table")

        Args:
            db_name: The name of the database to connect to.
            schema_name: The name of the schema to connect to. Primarily needed for Snowflake databases.
        """
        conn = None
        try:
            conn = self._open_connection(db_name, schema_name=schema_name)
            yield conn
        finally:
            if conn is not None:
                self._close_connection(connection=conn)

    def _open_connection(
        self, db_name: str, schema_name: Optional[str] = None
    ) -> SnowflakeConnection:
        connection = snowflake_connection(
            user=self._get_user(),
            password=self._get_password(),
            account=str(self.account_name),
            role=self._get_role(),
            warehouse=self._get_warehouse(),
            host=self._get_host(),
        )
        if db_name:
            try:
                connection.cursor().execute(f"USE DATABASE {db_name}")
            except Exception as e:
                raise ValueError(
                    f"Could not connect to database {db_name}. Does the database exist in {self.account_name}?"
                ) from e
        if schema_name:
            try:
                connection.cursor().execute(f"USE SCHEMA {schema_name}")
            except Exception as e:
                raise ValueError(
                    f"Could not connect to schema {schema_name}. Does the schema exist in the {db_name} database?"
                ) from e
        if _QUERY_TAG:
            connection.cursor().execute(f"ALTER SESSION SET QUERY_TAG = '{_QUERY_TAG}'")
        connection.cursor().execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {env_vars.DEFAULT_SESSION_TIMEOUT_SEC}"
        )
        return connection

    def _close_connection(self, connection: SnowflakeConnection) -> None:
        connection.close()

    def execute(
        self,
        connection: SnowflakeConnection,
        query: str,
    ) -> Dict[str, List[Any]]:
        try:
            if connection.warehouse is None:
                warehouse = self._get_warehouse()
                logger.debug(
                    f"There is no Warehouse assigned to Connection, setting it to config default ({warehouse})"
                )
                # TODO(jhilgart): Do we need to replace - with _?
                # Snowflake docs suggest we need identifiers with _, https://docs.snowflake.com/en/sql-reference/identifiers-syntax,
                # but unclear if we need this here.
                connection.cursor().execute(
                    f'use warehouse {warehouse.replace("-", "_")}'
                )
            cursor = connection.cursor(DictCursor)
            logger.info(f"Executing query = {query}")
            cursor_execute = cursor.execute(query)
            # assert below for MyPy. Should always be true.
            assert cursor_execute, "cursor_execute should not be None here"
            result = cursor_execute.fetchall()
        except ProgrammingError as e:
            raise ValueError(f"Query Error: {e}")

        out_dict = defaultdict(list)
        for row in result:
            if isinstance(row, dict):
                for k, v in row.items():
                    out_dict[k].append(v)
            else:
                raise ValueError(
                    f"Expected a dict for row object. Instead passed {row}"
                )
        return out_dict
