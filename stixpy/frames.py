import warnings

import astropy.coordinates as coord
import astropy.units as u
import numpy as np
from astropy.coordinates import frame_transform_graph
from astropy.coordinates.matrix_utilities import matrix_product, matrix_transpose, rotation_matrix
from astropy.io import fits
from astropy.table import QTable
from astropy.time import Time
from sunpy.coordinates.frames import HeliographicStonyhurst, Helioprojective, SunPyBaseCoordinateFrame
from sunpy.net import Fido
from sunpy.net import attrs as a

from stixpy.net.client import STIXClient  # noqa
from stixpy.utils.logging import get_logger

logger = get_logger(__name__, "DEBUG")

__all__ = ["STIXImaging", "_get_rotation_matrix_and_position", "hpc_to_stixim", "stixim_to_hpc"]

STIX_X_SHIFT = 26.1 * u.arcsec  # fall back to this when non sas solution available
STIX_Y_SHIFT = 58.2 * u.arcsec  # fall back to this when non sas solution available
STIX_X_OFFSET = 60.0 * u.arcsec  # remaining offset after SAS solution
STIX_Y_OFFSET = 8.0 * u.arcsec  # remaining offset after SAS solution


class STIXImaging(SunPyBaseCoordinateFrame):
    r"""
    STIX Imaging Frame

    - ``x`` (aka "theta_x") along the pixel rows (e.g. 0, 1, 2, 3; 4, 5, 6, 8).
    - ``y`` (aka "theta_y") along the pixel columns (e.g. A, B, C, D).
    - ``distance`` is the Sun-observer distance.

    Aligned with SIIX 'pixels' +X corresponds direction along pixel row toward pixels
    0, 4 and +Y corresponds direction along columns towards pixels 0, 1, 2, 3.

    .. code-block:: text

      Col\Row
            ---------
           | 3  | 7  |
        D  |   _|_   |
           |  |11 |  |
            ---------
           | 2  | 6  |
        C  |   _|_   |
           |  |10 |  |
            ---------
           | 1  | 5  |
        B  |   _|_   |
           |  | 9 |  |
            ---------
           | 0  | 4  |
        A  |   _|_   |
           |  | 8 |  |
            ---------

    """

    frame_specific_representation_info = {
        coord.SphericalRepresentation: [
            coord.RepresentationMapping("lon", "x", u.arcsec),
            coord.RepresentationMapping("lat", "y", u.arcsec),
            coord.RepresentationMapping("distance", "distance"),
        ]
    }

    # def make_3d(self):
    #     """
    #     This method calculates the third coordinate of the Helioprojective
    #     frame. It assumes that the coordinate point is on the surface of the Sun.
    #     If a point in the frame is off limb then NaN will be returned.
    #     Returns
    #     -------
    #     new_frame : `~sunpy.coordinates.frames.Helioprojective`
    #         A new frame instance with all the attributes of the original but
    #         now with a third coordinate.
    #     """
    #     # Skip if we already are 3D
    #     if not self._is_2d:
    #         return self
    #
    #     if not isinstance(self.observer, BaseCoordinateFrame):
    #         raise ConvertError("Cannot calculate distance to the Sun "
    #                            f"for observer '{self.observer}' "
    #                            "without `obstime` being specified.")
    #
    #     rep = self.represent_as(UnitSphericalRepresentation)
    #     lat, lon = rep.lat, rep.lon
    #
    #     # Check for the use of floats with lower precision than the native Python float
    #     if not set([lon.dtype.type, lat.dtype.type]).issubset([float, np.float64, np.longdouble]):
    #         warn_user("The Helioprojective component values appear to be lower "
    #                   "precision than the native Python float: "
    #                   f"Tx is {lon.dtype.name}, and Ty is {lat.dtype.name}. "
    #                   "To minimize precision loss, you may want to cast the values to "
    #                   "`float` or `numpy.float64` via the NumPy method `.astype()`.")
    #
    #     # Calculate the distance to the surface of the Sun using the law of cosines
    #     cos_alpha = np.cos(lat) * np.cos(lon)
    #     c = self.observer.radius ** 2 - self.rsun ** 2
    #     b = -2 * self.observer.radius * cos_alpha
    #     # Ignore sqrt of NaNs
    #     with np.errstate(invalid='ignore'):
    #         d = ((-1 * b) - np.sqrt(b ** 2 - 4 * c)) / 2  # use the "near" solution
    #
    #     if self._spherical_screen:
    #         sphere_center = self._spherical_screen['center'].transform_to(self).cartesian
    #         c = sphere_center.norm() ** 2 - self._spherical_screen['radius'] ** 2
    #         b = -2 * sphere_center.dot(rep)
    #         # Ignore sqrt of NaNs
    #         with np.errstate(invalid='ignore'):
    #             dd = ((-1 * b) + np.sqrt(b ** 2 - 4 * c)) / 2  # use the "far" solution
    #
    #         d = np.fmin(d, dd) if self._spherical_screen['only_off_disk'] else dd
    #
    #     # This warning can be triggered in specific draw calls when plt.show() is called
    #     # we can not easily prevent this, so we check the specific function is being called
    #     # within the stack trace.
    #     stack_trace = traceback.format_stack()
    #     matching_string = 'wcsaxes.*_draw_grid'
    #     bypass = any([re.search(matching_string, string) for string in stack_trace])
    #     if not bypass and np.all(np.isnan(d)) and np.any(np.isfinite(cos_alpha)):
    #         warn_user("The conversion of these 2D helioprojective coordinates to 3D is all NaNs "
    #                   "because off-disk coordinates need an additional assumption to be mapped to "
    #                   "calculate distance from the observer. Consider using the context manager "
    #                   "`Helioprojective.assume_spherical_screen()`.")
    #
    #     return self.realize_frame(SphericalRepresentation(lon=lon,
    #                                                       lat=lat,
    #                                                       distance=d))


def _get_rotation_matrix_and_position(obstime):
    r"""
    Return rotation matrix STIX Imaging to SOLO HPC and position of SOLO in HEEQ.

    Parameters
    ----------
    obstime : `astropy.time.Time`
        Time
    Returns
    -------
    """
    roll, solo_position_heeq, spacecraft_pointing = get_hpc_info(obstime)

    # Generate the rotation matrix using the x-convention (see Goldstein)
    # Rotate +90 clockwise around the first axis
    # STIX is mounted on the -Y panel (SOLO_SRF) need to rotate to from STIX frame to SOLO_SRF
    rot_to_solo = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])

    logger.debug("Pointing: %s.", spacecraft_pointing)
    A = rotation_matrix(-1 * roll, "x")
    B = rotation_matrix(spacecraft_pointing[1], "y")
    C = rotation_matrix(-1 * spacecraft_pointing[0], "z")

    # Will be applied right to left
    rmatrix = matrix_product(A, B, C, rot_to_solo)

    return rmatrix, solo_position_heeq


def get_hpc_info(obstime):
    roll, pitch, yaw, sas_x, sas_y, solo_position_heeq = _get_aux_data(obstime)
    if np.isnan(sas_x) or np.isnan(sas_y):
        warnings.warn("SAS solution not available using spacecraft pointing and average SAS offset")
        sas_x = yaw
        sas_y = pitch
    # Convert the spacecraft pointing to STIX frame
    rotated_yaw = -yaw * np.cos(roll) + pitch * np.sin(roll)
    rotated_pitch = yaw * np.sin(roll) + pitch * np.cos(roll)
    spacecraft_pointing = np.hstack([STIX_X_SHIFT + rotated_yaw, STIX_Y_SHIFT + rotated_pitch])
    sas_pointing = np.hstack([sas_x + STIX_X_OFFSET, -1 * sas_y + STIX_Y_OFFSET])
    pointing_diff = np.linalg.norm(spacecraft_pointing - sas_pointing)
    if pointing_diff < 200 * u.arcsec:
        logger.debug(f"Using SAS pointing {sas_pointing}")
        spacecraft_pointing = sas_pointing
    else:
        logger.debug(f"Using spacecraft pointing {spacecraft_pointing}")
        warnings.warn(
            f"Using spacecraft pointing large difference between "
            f"SAS {sas_pointing} and spacecraft {spacecraft_pointing}."
        )
    return roll, solo_position_heeq, spacecraft_pointing


def _get_aux_data(obstime):
    # Find, download, read aux file with pointing, sas and position information
    logger.debug("Searching for AUX data")
    query = Fido.search(
        a.Time(obstime, obstime), a.Instrument.stix, a.Level.l2, a.stix.DataType.aux, a.stix.DataProduct.aux_ephemeris
    )
    if len(query["stix"]) != 1:
        raise ValueError("Exactly one AUX file should be found but %d were found.", len(query["stix"]))
    logger.debug("Downloading AUX data")
    files = Fido.fetch(query)
    if len(files.errors) > 0:
        raise ValueError("There were errors downloading the data.")
    # Read and extract data
    logger.debug("Loading and extracting AUX data")
    hdu = fits.getheader(files[0], ext=0)
    aux = QTable.read(files[0], hdu=2)
    start_time = Time(hdu.get("DATE-BEG"))
    rel_date = (obstime - start_time).to("s")
    idx = np.argmin(np.abs(rel_date - aux["time"]))
    sas_x, sas_y = aux[idx][["y_srf", "z_srf"]]
    roll, pitch, yaw = aux[idx]["roll_angle_rpy"]
    solo_position_heeq = aux[idx]["solo_loc_heeq_zxy"]
    logger.debug(
        "SAS: %s, %s, Orientation: %s, %s, %s, Position: %s",
        sas_x,
        sas_y,
        roll,
        yaw.to("arcsec"),
        pitch.to("arcsec"),
        solo_position_heeq,
    )

    # roll, pitch, yaw = np.mean(aux[1219:1222]['roll_angle_rpy'], axis=0)
    # sas_x = np.mean(aux[1219:1222]['y_srf'])
    # sas_y = np.mean(aux[1219:1222]['z_srf'])

    return roll, pitch, yaw, sas_x, sas_y, solo_position_heeq


@frame_transform_graph.transform(coord.FunctionTransform, STIXImaging, Helioprojective)
def stixim_to_hpc(stxcoord, hpcframe):
    r"""
    Transform STIX Imaging coordinates to given HPC frame
    """
    logger.debug("STIX: %s", stxcoord)

    rot_matrix, solo_pos_heeq = _get_rotation_matrix_and_position(stxcoord.obstime)
    solo_heeq = HeliographicStonyhurst(solo_pos_heeq, representation_type="cartesian", obstime=stxcoord.obstime)

    # Transform from STIX imaging to SOLO HPC
    newrepr = stxcoord.cartesian.transform(rot_matrix)

    # Create SOLO HPC
    solo_hpc = Helioprojective(
        newrepr, obstime=stxcoord.obstime, observer=solo_heeq.transform_to(HeliographicStonyhurst)
    )
    logger.debug("SOLO HPC: %s", solo_hpc)

    # Transform from SOLO HPC to input HPC
    if hpcframe.observer is None:
        hpcframe = type(hpcframe)(obstime=hpcframe.obstime, observer=solo_heeq)
    hpc = solo_hpc.transform_to(hpcframe)
    logger.debug("Target HPC: %s", hpc)
    return hpc


@frame_transform_graph.transform(coord.FunctionTransform, Helioprojective, STIXImaging)
def hpc_to_stixim(hpccoord, stxframe):
    r"""
    Transform HPC coordinate to STIX Imaging frame.
    """
    logger.debug("Input HPC: %s", hpccoord)
    rmatrix, solo_pos_heeq = _get_rotation_matrix_and_position(stxframe.obstime)
    solo_hgs = HeliographicStonyhurst(solo_pos_heeq, representation_type="cartesian", obstime=stxframe.obstime)

    # Create SOLO HPC
    solo_hpc_frame = Helioprojective(obstime=stxframe.obstime, observer=solo_hgs)
    # Transform input to SOLO HPC
    solo_hpc_coord = hpccoord.transform_to(solo_hpc_frame)
    logger.debug("SOLO HPC: %s", solo_hpc_coord)

    # Transform from SOLO HPC to STIX imaging
    newrepr = solo_hpc_coord.cartesian.transform(matrix_transpose(rmatrix))
    stx_coord = stxframe.realize_frame(newrepr, obstime=solo_hpc_frame.obstime)
    logger.debug("STIX: %s", stx_coord)
    return stx_coord
