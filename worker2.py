# -*- coding: utf-8 -*-
"""
Created on Tue Aug 29 23:24:01 2023

@author: vpp
"""
from scipy.spatial import distance

def euclidean_distance(point1, point2):
    """Calculate the Euclidean distance between two points."""
    return distance.euclidean([point1.x, point1.y], [point2.x, point2.y])
