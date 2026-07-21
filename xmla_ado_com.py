"""
XMLA connectivity helper for Power BI using Windows ADO COM + MSOLAP.

Why this module exists:
- The old package used `adodbapi`, but that package is difficult to install on
  modern Python/setuptools versions because it still depends on deprecated
  distutils `build_py_2to3` build behavior.
- This module avoids the `adodbapi` pip build step and talks to the same
  Microsoft ADO COM layer directly through `pywin32`.

Runtime requirements:
- Windows runtime
- pywin32 Python package
- Microsoft Analysis Services OLE DB Provider installed on the machine
- Power BI XMLA endpoint enabled for the workspace/capacity
"""

from __future__ import annotations

import platform
from typing import Any, Iterable, List, Optional, Sequence, Tuple


class AdoComConnection:
    """Small wrapper around an ADODB.Connection COM object."""

    def __init__(self, conn_str: str):
        self._conn = None
        self._pythoncom = None
        self._com_initialized = False
        try:
            import pythoncom  # type: ignore
            import win32com.client  # type: ignore
        except Exception as exc:  # pragma: no cover - Windows-only dependency
            if platform.system() != "Windows":
                raise RuntimeError(
                    "XMLA mode uses Windows COM through pywin32 and the Microsoft MSOLAP provider. "
                    "Streamlit Community Cloud runs on Linux, so pywin32 cannot be installed or used there. "
                    "Use REST-based features on Streamlit Cloud, or run XMLA lineage on a Windows host with "
                    "pywin32 and MSOLAP installed."
                ) from exc
            raise RuntimeError(
                "pywin32 is required for XMLA mode. Install it with: pip install pywin32, "
                "and make sure the Microsoft Analysis Services OLE DB Provider is installed."
            ) from exc

        self._pythoncom = pythoncom
        try:
            pythoncom.CoInitialize()
            self._com_initialized = True
            self._conn = win32com.client.Dispatch("ADODB.Connection")
            self._conn.Open(conn_str)
        except Exception:
            self.close()
            raise

    def cursor(self) -> "AdoComCursor":
        return AdoComCursor(self)

    def execute(self, query: str):
        return self._conn.Execute(query)

    def close(self) -> None:
        try:
            if self._conn:
                self._conn.Close()
        except Exception:
            pass
        finally:
            self._conn = None
            if self._com_initialized and self._pythoncom is not None:
                try:
                    self._pythoncom.CoUninitialize()
                except Exception:
                    pass
                self._com_initialized = False


class AdoComCursor:
    """DB-API-like cursor wrapper used by the existing Streamlit code."""

    def __init__(self, connection: AdoComConnection):
        self.connection = connection
        self.description: Optional[List[Tuple[str]]] = None
        self._rows: List[Tuple[Any, ...]] = []

    def execute(self, query: str) -> None:
        recordset, _records_affected = self.connection.execute(query)
        self._rows = []
        self.description = []

        if recordset is None:
            return

        try:
            field_count = int(recordset.Fields.Count)
            field_names = [str(recordset.Fields.Item(i).Name) for i in range(field_count)]
            self.description = [(name,) for name in field_names]

            while not bool(recordset.EOF):
                row = tuple(recordset.Fields.Item(i).Value for i in range(field_count))
                self._rows.append(row)
                recordset.MoveNext()
        finally:
            try:
                recordset.Close()
            except Exception:
                pass

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        self._rows = []
        self.description = None


def connect_xmla(conn_str: str) -> AdoComConnection:
    """
    Open an XMLA connection through ADODB/MSOLAP and return a DB-API-like
    connection wrapper.
    """
    return AdoComConnection(conn_str)
