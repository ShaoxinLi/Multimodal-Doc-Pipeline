__version__ = "0.1.0"

from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import InputFile, PipelineError, SourceIdentity
from doc_pipeline.contracts.run import ProgressEvent
from doc_pipeline.pipeline import process_file, process_files

__all__ = [
    "Config", "InputFile", "PipelineError", "ProgressEvent",
    "SourceIdentity", "process_file", "process_files",
]
