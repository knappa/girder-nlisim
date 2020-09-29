import json
from logging import getLogger
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, Dict
from urllib.request import urlopen

import attr
from celery import Task
from girder_client import GirderClient, HttpError
from girder_jobs.constants import JobStatus

from girder_nlisim.celery import app
from nlisim.config import SimulationConfig
from nlisim.postprocess import generate_vtk
from nlisim.solver import run_iterator, Status

logger = getLogger(__name__)
GEOMETRY_FILE_URL = (
    'https://data.nutritionallungimmunity.org/api/v1/file/5ebd86cec1b2cfe0661e681f/download'
)


@attr.s(auto_attribs=True, kw_only=True)
class GirderConfig:
    """Configure where the data from a simulation run is posted."""

    #: authentication token
    token: str

    #: root folder id where the data will be placed
    folder: str

    #: base api url
    api: str = 'https://data.nutritionallungimmunity.org/api/v1'

    @property
    def client(self) -> GirderClient:
        cl = GirderClient(apiUrl=self.api)
        cl.token = self.token
        return cl

    def upload_config(self, simulation_id: str, simulation_config: SimulationConfig):
        client = self.client
        with NamedTemporaryFile('w') as f:
            simulation_config.write(f)
            f.flush()
            client.uploadFileToFolder(simulation_id, f.name, filename='config.ini')

    def initialize(self, name: str, target_time: float, simulation_config: SimulationConfig):
        simulation = self.client.post(
            'nli/simulation',
            parameters={
                'name': name,
                'folderId': self.folder,
                'config': json.dumps(
                    {
                        'targetTime': target_time,
                    }
                ),
            },
        )
        self.upload_config(simulation['_id'], simulation_config)
        return simulation

    def finalize(self, simulation_id: str):
        return self.client.post(f'nli/simulation/{simulation_id}/complete')

    def set_status(self, job_id: str, status: int, current: float, total: float):
        return self.client.put(
            f'job/{job_id}',
            parameters={
                'status': status,
                'progressTotal': total,
                'progressCurrent': current,
            },
        )

    def upload(self, simulation_id: str, name: str, directory: Path) -> str:
        """Upload files to girder and return the created folder id."""
        client = self.client
        logger.info(f'Uploading to {name}')
        folder = client.createFolder(simulation_id, name)['_id']
        for file in directory.glob('*'):
            self.client.uploadFileToFolder(folder, str(file))
        return folder


def download_geometry():
    geometry_file_path = Path('geometry.hdf5')
    if not geometry_file_path.is_file():
        with urlopen(GEOMETRY_FILE_URL) as f, geometry_file_path.open('wb') as g:
            # TODO: stream into file if it becomes large
            g.write(f.read())


@app.task(bind=True)
def run_simulation(
    self: Task,
    girder_config: GirderConfig,
    simulation_config: SimulationConfig,
    name: str,
    target_time: float,
    job: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a simulation and export postprocessed vtk files to girder."""
    current_time = 0
    logger.info('initialize')
    try:
        simulation = girder_config.initialize(name, target_time, simulation_config)
        girder_config.set_status(job['_id'], JobStatus.RUNNING, current_time, target_time)

        download_geometry()
        time_step = 0

        for state, status in run_iterator(simulation_config, target_time):
            current_time = state.time
            logger.info(f'Simulation time {state.time}')
            with TemporaryDirectory() as temp_dir:
                temp_dir_path = Path(temp_dir)
                generate_vtk(state, temp_dir_path)

                step_name = '%03i' % time_step if status != Status.finalize else 'final'
                girder_config.upload(simulation['_id'], step_name, temp_dir_path)
                girder_config.set_status(job['_id'], JobStatus.RUNNING, current_time, target_time)

            time_step += 1

        girder_config.finalize(simulation['_id'])
        girder_config.set_status(job['_id'], JobStatus.SUCCESS, target_time, target_time)
        return simulation
    except HttpError as e:
        logger.error('Error communicating with girder')
        logger.error(e.responseText)
        raise
    except Exception:
        try:
            girder_config.set_status(job['_id'], JobStatus.ERROR, current_time, target_time)
        except Exception:
            logger.exception('Could not set girder error status')
        raise
