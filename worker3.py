# -*- coding: utf-8 -*-
"""
Created on Wed Aug 30 09:56:48 2023

@author: vpp
"""
from geopy.distance import geodesic

def calculate_distance(coord1, coord2):
    """
    Calculate the distance (in kilometers) between two sets of (lat, long) coordinates.
    
    Parameters:
    - coord1: Tuple of (latitude, longitude) for the first point.
    - coord2: Tuple of (latitude, longitude) for the second point.
    
    Returns:
    - Distance in kilometers.
    """
    return geodesic(coord1, coord2).kilometers




