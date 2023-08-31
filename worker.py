# -*- coding: utf-8 -*-
"""
Created on Sat Aug 26 13:49:00 2023

@author: vpp
"""
import numpy as np
import osmnx as ox
import osmnx
import networkx as nx

from multiprocessing import Pool, cpu_count

def calculate_path(args):
    G, orig_point, dest_point, i, j = args
    
    weight = 'length'
    
    # Get the nearest nodes to the points
    orig_node = ox.distance.nearest_nodes(G, orig_point.x, orig_point.y)
    dest_node = ox.distance.nearest_nodes(G, dest_point.x, dest_point.y)
    
    try:
        # Get the shortest path using OSMnx
        path = ox.distance.shortest_path(G, orig_node, dest_node, weight=weight, cpus=4)
        
        if path is None:  # Handling the None case here
            print(f"No valid path found for origin {i} and destination {j}")
            return i, j, 10000  # Return the default value
        
        # Calculate the path length based on the edges in the path
        path_edges = list(zip(path[:-1], path[1:]))
        path_length = sum([G[u][v][0][weight] for u, v in path_edges])
        print(path_length)
        return i, j, path_length
        
    except nx.NetworkXNoPath:
        #print(f"No path found for origin {i} and destination {j}")
        return i, j, 10000
    except Exception as e:  # A general exception to catch other unexpected issues
        print(f"Error for origin {i} and destination {j}: {e}")
        return i, j, 10000


