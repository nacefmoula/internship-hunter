"""Source registry. Maps a CLI ``--source`` name to its Source class."""

from .base import Source
from .emploitunisie import EmploiTunisie
from .himalayas import Himalayas
from .hn_hiring import HNHiring
from .keejob import Keejob
from .remoteok import RemoteOK
from .remotive import Remotive
from .summer_internships import SummerInternships
from .tanitjobs import TanitJobs
from .wwr import WeWorkRemotely

REGISTRY: dict[str, type[Source]] = {
    RemoteOK.name: RemoteOK,
    Remotive.name: Remotive,
    WeWorkRemotely.name: WeWorkRemotely,
    Himalayas.name: Himalayas,
    HNHiring.name: HNHiring,
    Keejob.name: Keejob,
    EmploiTunisie.name: EmploiTunisie,
    TanitJobs.name: TanitJobs,
    SummerInternships.name: SummerInternships,
}

#: source name -> posting language ("en"/"fr"). Drives CV + cover-letter
#: language selection in the drafter.
SOURCE_LANG: dict[str, str] = {name: cls.language for name, cls in REGISTRY.items()}

__all__ = [
    "Source",
    "REGISTRY",
    "SOURCE_LANG",
    "RemoteOK",
    "Remotive",
    "WeWorkRemotely",
    "Himalayas",
    "HNHiring",
    "Keejob",
    "EmploiTunisie",
    "TanitJobs",
    "SummerInternships",
]
