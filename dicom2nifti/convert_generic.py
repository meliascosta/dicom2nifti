# -*- coding: utf-8 -*-
"""
dicom2nifti

@author: abrys
"""

from __future__ import print_function
import dicom2nifti.patch_pydicom_encodings

dicom2nifti.patch_pydicom_encodings.apply()

import logging
import nibabel
import numpy

from pydicom.tag import Tag

import six

import dicom2nifti.common as common
import dicom2nifti.settings as settings
import dicom2nifti.resample as resample
from dicom2nifti.exceptions import ConversionError

logger = logging.getLogger(__name__)


def dicom_to_nifti(dicom_input, output_file):
    """
    This function will convert an anatomical dicom series to a nifti

    Examples: See unit test

    :param output_file: filepath to the output nifti
    :param dicom_input: directory with the dicom files for a single scan, or list of read in dicoms
    """
    if len(dicom_input) <= 0:
        raise ConversionError('NO_DICOM_FILES_FOUND')

    # remove duplicate slices based on position and data
    dicom_input = _remove_duplicate_slices(dicom_input)

    # remove localizers based on image type
    dicom_input = _remove_localizers_by_imagetype(dicom_input)
    if settings.validate_slicecount:
        # remove_localizers based on image orientation (only valid if slicecount is validated)
        dicom_input = _remove_localizers_by_orientation(dicom_input)

        # validate all the dicom files for correct orientations
        common.validate_slicecount(dicom_input)
    if settings.validate_orientation:
        # validate that all slices have the same orientation
        common.validate_orientation(dicom_input)
    if settings.validate_orthogonal:
        # validate that we have an orthogonal image (to detect gantry tilting etc)
        common.validate_orthogonal(dicom_input)

    # sort the dicoms
    dicom_input = common.sort_dicoms(dicom_input)

    # validate slice increment inconsistent
    slice_increment_inconsistent = False
    if settings.validate_slice_increment:
        # validate that all slices have a consistent slice increment
        common.validate_slice_increment(dicom_input)
    elif common.is_slice_increment_inconsistent(dicom_input):
        slice_increment_inconsistent = True

    # if inconsistent increment and we allow resampling then do the resampling based conversion to maintain the correct geometric shape
    if slice_increment_inconsistent and settings.resample:
        nii_image, max_slice_increment = _convert_slice_incement_inconsistencies(dicom_input)
    # do the normal conversion
    else:
        # Get data; originally z,y,x, transposed to x,y,z
        data = common.get_volume_pixeldata(dicom_input)

        affine, max_slice_increment = common.create_affine(dicom_input)

        # Convert to nifti
        nii_image = nibabel.Nifti1Image(data, affine)

    # Set TR and TE if available
    if Tag(0x0018, 0x0080) in dicom_input[0] and Tag(0x0018, 0x0081) in dicom_input[0]:
        common.set_tr_te(nii_image, float(dicom_input[0].RepetitionTime), float(dicom_input[0].EchoTime))

    # Save to disk
    if output_file is not None:
        logger.info('Saving nifti to disk %s' % output_file)
        nii_image.to_filename(output_file)

    return {'NII_FILE': output_file,
            'NII': nii_image,
            'MAX_SLICE_INCREMENT': max_slice_increment}


def _remove_duplicate_slices(dicoms):
    """
    Search dicoms for localizers and delete them
    """
    # Loop overall files and build dict

    dicoms_dict = {}
    filtered_dicoms = []
    for dicom_ in dicoms:
        if tuple(dicom_.ImagePositionPatient) not in dicoms_dict:
            dicoms_dict[tuple(dicom_.ImagePositionPatient)] = dicom_
            filtered_dicoms.append(dicom_)
        else:
            if numpy.array_equal(dicom_.pixel_array,
                                 dicoms_dict[tuple(dicom_.ImagePositionPatient)].pixel_array):
                logger.warning('Removing duplicate slice from series')
            else:
                filtered_dicoms.append(dicom_)
    return filtered_dicoms


def _remove_localizers_by_imagetype(dicoms):
    """
    Search dicoms for localizers and delete them
    """
    # Loop overall files and build dict
    filtered_dicoms = []
    for dicom_ in dicoms:
        if 'ImageType' in dicom_ and 'LOCALIZER' in dicom_.ImageType:
            continue
        # 'Projection Image' are Localizers for CT only see MSMET-234
        if 'CT' in dicom_.Modality and 'ImageType' in dicom_ and 'PROJECTION IMAGE' in dicom_.ImageType:
            continue
        filtered_dicoms.append(dicom_)
    return filtered_dicoms


def _remove_localizers_by_orientation(dicoms):
    """
    Removing localizers based on the orientation.
    This is needed as in some cases with ct data there are some localizer/projection type images that cannot
    be distiguished by the dicom headers. This is why we kick out all orientations that do not have more than 4 files
    4 is the limit anyway for converting to nifti on our case
    """
    orientations = []
    sorted_dicoms = {}
    # Loop overall files and build dict
    for dicom_header in dicoms:
        # Create affine matrix (http://nipy.sourceforge.net/nibabel/dicom/dicom_orientation.html#dicom-slice-affine)
        image_orient1 = numpy.array(dicom_header.ImageOrientationPatient)[0:3]
        image_orient2 = numpy.array(dicom_header.ImageOrientationPatient)[3:6]
        image_orient_combined = (image_orient1.tolist(), image_orient2.tolist())
        found_orientation = False
        for orientation in orientations:
            if numpy.allclose(image_orient_combined[0], numpy.array(orientation[0]), rtol=0.001, atol=0.001) \
                    and numpy.allclose(image_orient_combined[1], numpy.array(orientation[1]), rtol=0.001,
                                       atol=0.001):
                sorted_dicoms[str(orientation)].append(dicom_header)
                found_orientation = True
                break
        if not found_orientation:
            orientations.append(image_orient_combined)
            sorted_dicoms[str(image_orient_combined)] = [dicom_header]

    # if there are multiple possible orientations delete orientations where there are less than 4 files
    # we don't convert anything less that that anyway

    if len(sorted_dicoms) > 1:
        filtered_dicoms = []
        for orientation in sorted_dicoms.keys():
            if len(sorted_dicoms[orientation]) >= 4:
                filtered_dicoms.extend(sorted_dicoms[orientation])
        return filtered_dicoms
    else:
        return six.next(six.itervalues(sorted_dicoms))


def _convert_slice_incement_inconsistencies(dicom_input):
    """
    If there is slice increment inconsistency detected, for the moment CT images, then split the volumes into subvolumes based on the slice increment and process each volume separately using a space constructed based on the highest resolution increment
    """

    #   Estimate the "first" slice increment based on the 2 first slices
    increment = numpy.array(dicom_input[0].ImagePositionPatient) - numpy.array(dicom_input[1].ImagePositionPatient)

    # Create as many volumes as many changes in slice increment. NB Increments might be repeated in different volumes
    max_slice_increment = 0
    slice_incement_groups = []
    current_group = [dicom_input[0], dicom_input[1]]
    previous_image_position = numpy.array(dicom_input[1].ImagePositionPatient)
    for dicom in dicom_input[2:]:
        current_image_position = numpy.array(dicom.ImagePositionPatient)
        current_increment = previous_image_position - current_image_position
        max_slice_increment = max(max_slice_increment, numpy.linalg.norm(current_increment))
        if numpy.allclose(increment, current_increment, rtol=0.05, atol=0.1):
            current_group.append(dicom)
        if not numpy.allclose(increment, current_increment, rtol=0.05, atol=0.1):
            slice_incement_groups.append(current_group)
            current_group = [current_group[-1], dicom]
            increment = current_increment
        previous_image_position = current_image_position
    slice_incement_groups.append(current_group)

    # Create nibabel objects for each volume based on the corresponding headers
    slice_incement_niftis = []
    slice_increments = []
    voxel_sizes = {}
    for dicom_slices in slice_incement_groups:
        data = common.get_volume_pixeldata(dicom_slices)
        affine, _ = common.create_affine(dicom_slices)
        current_volume = nibabel.Nifti1Image(data, affine)
        slice_increment = numpy.linalg.norm(current_volume.header.get_zooms())
        voxel_sizes['%.5f' % slice_increment] = current_volume.header.get_zooms()
        slice_increments.extend([slice_increment] * (len(dicom_slices)-1))
        slice_incement_niftis.append(current_volume)
    voxel_size = voxel_sizes['%.5f' % numpy.percentile(slice_increments, 10)]

    nifti_volume = resample.resample_nifti_images(slice_incement_niftis, voxel_size=voxel_size)

    return nifti_volume, max_slice_increment
