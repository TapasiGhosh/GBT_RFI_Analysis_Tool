"""
.. module:: RFI_input_for_SQL.py
    :synopsis: To read in multiple types of ascii files containing RFI data across several decades. Then output in a form usable by a mySQL database.
.. moduleauthor:: Joy Skipper <jskipper@nrao.edu>
Code Origin: https://github.com/JoySkipper/GBT_RFI_Analysis_Tool
"""

import numpy as np
import matplotlib.pyplot as plt
import os
import sys
import copy
import re
import julian
import datetime
import rfitrends.LST_calculator
from mysql import connector
import getpass
import rfitrends.GBT_receiver_specs
import sys
import rfitrends.fxns_output_process
import argparse
import math
import rfitrends.Column_fixes
import configparser
from rfitrends.manage_missing_cols import manage_missing_cols
import json
import mysql

class FreqOutsideRcvrBoundsError(Exception):
    pass

class InvalidColumnValues(Exception):
    pass

class InvalidIntensity(Exception):
    pass

class DuplicateValues(Exception):
    pass



def read_file(filepath,main_database,dirty_database, cursor):#use this function to read in a particular file and return a dictionary with all header values and lists of the data
    """
    Goes through each file, line by line, processes and cleans the data, then loads it into a dictionary with a marker for the corresponding database
    to which it belongs. 

    param main_database: the primary database to which the person wants their clean data to go
    param dirty_database: the secondary database to which the person wans their "dirty," or nonsensical data to go
    param cursor: The connection to the database
    returns formatted_RFI_file: The dictionary with all of the data formatted and organized. 
    returns header_map: contains all information, not just header
    """
    
    f = open(filepath, 'r') #open file
    #these are lists containing column values that will be added to the dictionary later:
    data = {}
    database = []
    if '#' in f.read(1):
        has_header = True
    else:
        has_header = False
    f.seek(0)


    if has_header:
        def process_header(file):
            f = file
            # Dict of header fields and values
            header = {}
            previous_line_position = f.tell()
            # emulating a do while loop because it doesn't exist in Python
            line = f.readline()
            # Read the file
            while line:
                # Header entries are denoted via '#' symbol
                if '#' in line:
                    # Standard header entries are denoted via "key: value" 
                    try:
                        header_entry = line.strip('#').strip().split(":")
                        header[header_entry[0]] = header_entry[1].strip()
                    except:
                        # If not, there are two possibilities:
                        # (1) Title lines (meant to be skipped)
                        # (2) Column names (in which case it is directly preceded by data (non header line))

                        # Peek ahead at next line
                        current_position = f.tell()
                        # If there is no '#' symbol, the next line is data, therefore this line denotes the column names
                        first_data_line = f.readline()
                        if "#" not in first_data_line:
                            # Assumes column names are separated by variable number of spaces/tabs
                            column_entries = line.strip('#').split()
                            header['Column names'] = column_entries

                        # Otherwise it's either a title line, or we don't support the syntax. Regardless, we should skip it
                        else:
                            #print("Skipping header line: " + line)
                            pass
                        # Undo our peek so we can process appropriately
                        f.seek(current_position)

                # If there is no header indicator ('#'), we've reached our data entries
                # Works under assumption that data entries are last in file
                else:
                    # Revert to last line and exit loop. We've finished parsing our header
                    f.seek(previous_line_position)
                    break

                # Again emulating a do while loop
                previous_line_position = f.tell()
                line = f.readline()

            return(header,first_data_line)
        header_map,first_data_line = process_header(f)
    else:
        header_map = extrapolate_header(f.name)
        # Get the first line 
        first_data_line = f.readline()
        # Set the reader back to the first line
        f.seek(0)

    # Verifies that frontend given exists, otherwise labels it as Unknown. 
    header_map["frontend"] = rfitrends.GBT_receiver_specs.FrontendVerification(header_map["frontend"])
    # Pulls filename from full path to filename
    header_map["filename"] = filepath.split("/")[-1]
    # Loop until we find the first valid data line, which we need to lookup the primary key:


    last_pos = f.tell()
    if first_data_line == '\n':
        first_data_line = f.readline()
    while True:
        try:
            first_data_entry = ReadFileLine_ColumnValues(has_header, first_data_line.strip().split(), header_map['Column names'], f.name)
        except InvalidIntensity:
            # If the velocity is invalid, then continue to read the next line:
            last_pos = f.tell()
            first_data_line = f.readline()
            continue

        break
    f.seek(last_pos)
    try:
        first_data_entry["Frequency_MHz"] = FrequencyVerification(first_data_entry["Frequency_MHz"],header_map)    
    # IF we do get frequencies outside of the bounds that we want, we skip to the next line
    except FreqOutsideRcvrBoundsError:
        # This is a dirty table. The error handling below will catch it, so we just need the old frequency value. We mainly want the verification and change 
        # Here in case the line is valid and needs tweaking (GHz to MHz, for example) 
        # Assumes that if one value in a file is bad, that they all will be
        pass
        
        

    
    # We got the first line so we can look up with the primary key to see if this primary key has somehow been entered before: 
    # We check for existing filenames, but there is an instance of the same frequency, and intensity somehow being tagged under different filenames
    # This error has not been able to be replicated, as it hasn't occured since 2017 and the data was not archived
    # IF THIS ERROR SHOWS, PLEASE CONTACT THE MAINTAINER OF THIS CODE. Then we can replicate the issue and possibly fix it. 

    first_line_entry = dict(header_map)
    first_line_entry.update(first_data_entry)
    # Getting primary composite key from config file:
    config = configparser.ConfigParser()
    config.read("rfitrends.conf")
    composite_keys = json.loads(config['Mandatory Fields']['primary_composite_key'])
    search_query = "SELECT * from "+main_database+" WHERE "
    # Searching by all the values in the composite key
    for composite_key in composite_keys:
        search_query += composite_key+" = "+str(first_line_entry[composite_key])+" AND "
    # Removing last " AND "
    search_query = search_query[:-4]
    # Execute query and see if there's a duplicate primary key with the first line and the database. If so, raise error
    cursor.execute(search_query)
    myresult = cursor.fetchall()
    if myresult:
        raise DuplicateValues
    
    
    for data_line in f:
        if data_line == '\n':
            continue
        try:
            data_entry = ReadFileLine_ColumnValues(has_header, data_line.strip().split(), header_map['Column names'], f.name)
        # If the data was flagged for invalid intensity, skip it. Not useful for science. 
        except InvalidIntensity:
            continue

        try:
            data_entry["Frequency_MHz"] = FrequencyVerification(data_entry["Frequency_MHz"],header_map)
            database.append(main_database)
            database_value = main_database
            
        # IF we do get frequencies outside of the bounds that we want, we put it into the dirty table. 
        except FreqOutsideRcvrBoundsError:
            database.append(dirty_database)
            database_value = dirty_database

        data_entry["Database"] = database_value

        # If data entry is already in data, we have a repeat value, then we just up the counts
        
        if data_entry["Frequency_MHz"] in data:
            data[data_entry["Frequency_MHz"]]["Counts"] += 1
        
        # If it's not in there, then we know there's one of them, the one we just found. Then we append it to the data list. 
        else:
            frequency_key = data_entry["Frequency_MHz"]
            del data_entry["Frequency_MHz"]
            data_entry['Counts'] = 1
            data[frequency_key] = data_entry


    header_map['Data'] = data
    return(header_map)

def FrequencyVerification(frequency_value,header):
    """
    Identifies issues in the frequency value in RFI-data
    1.) Checks for units (MHz vs GHz)
    2.) Checks that the frequency is in the appropriate range for the given receiver, under the assumption that
    they were generated by the understood receivers prior to 2019
    
    :param frequency_value: the frequency value to verify
    :param header_information: the dictionary made with header information for each file 
    :returns validated_frequency: the validated frequency value
    """
    # Makes the assumption that we're not observing below 245 MHz
    if float(frequency_value) < 245.0: # Converting all GHz values to MHz
        validated_frequency = str(float(frequency_value) * 1000.0)
    else:
        validated_frequency = frequency_value


    freq_min = rfitrends.GBT_receiver_specs.GBT_receiver_ranges[header["frontend"]]['freq_min']
    freq_max = rfitrends.GBT_receiver_specs.GBT_receiver_ranges[header["frontend"]]['freq_max']

    # If we don't know the receiver, then we can't give a required frequency range.
    if rfitrends.GBT_receiver_specs.GBT_receiver_ranges[header["frontend"]] == "Unknown": 
        freq_buffer = 0
    else:
        buffer_factor = .1
        # If we do know the receiver, then we take the range of that receiver and allow 1/10th of that range on either end to be included for
        # That receiver
        freq_buffer = (freq_max - freq_min)* buffer_factor
    if float(validated_frequency) < (freq_min - freq_buffer) or float(validated_frequency) > (freq_max + freq_buffer):
        raise FreqOutsideRcvrBoundsError
    return validated_frequency

def extrapolate_header(filepath):
    """
    Gleans as much information that would normally be in a header from a file that has been determined by the read_file function to not have a header 
    and populates it into that file's dictionary.

    param filepath: The path to that particular file

    returns extrapolated_header: dict with extrapolated header information derived from file name
    """
    extrapolated_header = {}
    
    #Gleaning information from a file that does not contain a file header for information
    filename = (filepath.split("/")[-1])# splitting filepath back down to just the filename    
    extrapolated_header.update({"filename": filename})
    filename_temporary = re.split('[_.]',filename)#split the filename into the naming components (there's no header, so we have to glean info from the filename)
    filename = filename_temporary

    unix_timestamp = (os.path.getmtime(filepath))
    date = (datetime.datetime.utcfromtimestamp(unix_timestamp))
    extrapolated_header.update({"date": (date.strftime('%Y-%m-%d %H:%M:%S'))})# gleaning info from filename
    #Calculating MJD...
    jd = julian.to_jd(date+ datetime.timedelta(hours=12),fmt='jd')
    mjd = jd  - 2400000.5
    extrapolated_header.update({"mjd": mjd})
    extrapolated_header.update({"azimuth (deg)":float(filename[7][2:])})
    extrapolated_header.update({"elevation (deg)":float(filename[8][2:])})
    extrapolated_header.update({"feed": "NaN"})
    extrapolated_header.update({"frontend": str(filename[2])})
    extrapolated_header.update({"projid": "NaN"})
    extrapolated_header.update({"frequency_resolution (MHz)": "NaN"})
    extrapolated_header.update({"Window": "NaN"})
    extrapolated_header.update({"exposure": "NaN"})
    utc_hr = (float(date.strftime("%H")))
    utc_min = (float(date.strftime("%M"))/60.0 )
    utc_sec = (float(date.strftime("%S"))/3600.0)
    utc = utc_hr+utc_min+utc_sec
    extrapolated_header.update({"utc (hrs)": utc})
    extrapolated_header.update({"number_IF_Windows": "NaN"})
    extrapolated_header.update({"Channel": "NaN"})
    extrapolated_header.update({"backend": "NaN"})

    year_formatted = date.strftime('%Y')[2:]
    utc_formatted = date.strftime('%m%d'+year_formatted+' %H%M')
    LSThh,LSTmm,LSTss = rfitrends.LST_calculator.LST_calculator(utc_formatted)
    LST = LSThh + LSTmm/60.0 + LSTss/3600.0
    extrapolated_header.update({"lst (hrs)": LST})

    extrapolated_header.update({"polarization":filename[6]})
    extrapolated_header.update({"source":"NaN"})
    extrapolated_header.update({"tsys":"NaN"})
    extrapolated_header.update({"frequency_type":"NaN"})
    extrapolated_header.update({"Units":"Jy"})
    extrapolated_header.update({"scan_number":"NaN"})

    extrapolated_header['Column names'] = ["Frequency (MHz)","Intensity (Jy)"]
    return(extrapolated_header)

    
def ReadFileLine_ColumnValues(has_header,line_value: list,column_names,filepath):
    """
    Reads one line in a file that has been determined by the read_file function to be a row with data as opposed to header information
    param has_header: boolean determining if the file has a header or not
    param line_value: the parsed values containing the information for the particular line of this file 
    param column_names: the names of the columns contained in this file
    param filepath: the path to this particular file


    returns data_entry: This is a dictionary containing either the column data or one key called "Flagged" which is set to true, an indication to throw away the data.

    """
    # Unfortunately, we first have to check if the column names match the length of the column values. For example, if the columns overlapped with themselves anywhere, such as the frequency values bleeding into intensity values to make 1471.456800.000 which should be 
    # something like 1471.456 for frequency and 800.000 for intensity or something (these are made up numbers for example only). 
    if len(column_names) != len(line_value):
        raise InvalidColumnValues("The number of column names and number of column values for this file is not equal. This is an invalid file.")
    # Next we need to streamline the naming conventions for the columns:    
    fixed_column_names = []
    for column_name in column_names:
        try: 
            fixed_column_name = rfitrends.Column_fixes.Column_name_corrections[column_name]
        except:
            raise InvalidColumnValues("There is an unrecognized column name "+column_name+". Please check and reformat your file or add it to the list of column names in Column_fixes.py")
        fixed_column_names.append(fixed_column_name)
    # We also need to check that required columns in the conf file exist somewhere in these columns, as they're needed for any science: 
    config = configparser.ConfigParser()
    config.read("rfitrends.conf")
    mandatory_columns = json.loads(config['Mandatory Fields']['mandatory_columns'])
    for mandatory_column in mandatory_columns:
        if mandatory_column not in fixed_column_names:
            raise InvalidColumnValues("One of the manditory columns listed in rfitrends.conf is not present in this file. This is required to continue processing this file.")

    # now that we know this is a correctly made line, we can get the data from the lines: 
    data_entry  = dict(zip(fixed_column_names,line_value))

    # Finally, we need to throw away this line if Intensity is NaN, as it's not a useful line for science: 
    intensity_isNaN = math.isnan(float(data_entry["Intensity_Jy"]))
    if intensity_isNaN:
        raise InvalidIntensity()

    # Okay, so there's nothing wrong with the line, so we can actually return a normal line: 
    return data_entry

def prompt_user_login_to_database(IP_address, database):
    
    while True:
        try:
            print("Connecting to database: " + str(database) + " on host: " + str(IP_address))
            username = input("Please enter SQL database username: ")
            password = getpass.getpass("Please enter the password: ",stream=None)
            connector.connect(user=username, password=password,
                                host=IP_address,
                                database=database)
            return(username, password)
        except:
            print("Incorrect username or password. Please try again.")

"""
def reconnect(username,password,IP_address,database):
    cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
    cursor = cnx.cursor(buffered=True)
    return(cursor)
"""

def write_to_database(username,password,IP_address,database,main_table,dirty_table,path,files_to_process = "all"):

    list_o_paths = []

    if files_to_process == "all":
    # making a list of all of the .txt files in the directory so I can just cycle through each full path:
        for filename in os.listdir(path):
            if filename.endswith(".txt") and filename != "URLs.txt":# If the files are ones we are actually interested in
                list_o_paths.append(os.path.join(path,filename))
                continue
    else: 
        # For each file in the path given
        for filename in os.listdir(path):
            # If there is any element from files_to_process contained in the current filename, it is a file to process. I.E. if "TRFI_052819_L1" is 
            # An element in files_to_process, and filename is "TRFI_052819_L1_rfiscan1_s0001_f001_Linr_az357_el045.txt" then it will be included as a file to process
            if any(RFI_file in filename for RFI_file in files_to_process):
                list_o_paths.append(os.path.join(path,filename))


    
    # cursor = reconnect(username,password,IP_address,database)
    cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
    cursor = cnx.cursor(buffered=True)


    print("gathering filename set (this takes a few minutes)")

    unique_filename = rfitrends.fxns_output_process.gather_list(cursor, "SELECT DISTINCT filename FROM "+main_table)

    cursor.close()

    #going thru each file one by one
    print("starting to upload files one by one...")
    for filenum,filepath in enumerate(list_o_paths):
        print("Extracting file "+str(filenum+1)+" of "+str(len(list_o_paths))+", filename: "+str(filepath))
        filename = filepath.split("/")[-1] # Getting filename from last piece in file path
        if filename in unique_filename:
            print("File already exists in database, moving on to next file.")
            continue
        try:
            cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
            cursor = cnx.cursor(buffered=True)
            formatted_RFI_file = read_file(filepath,main_table,dirty_table,cursor)
            cursor.close()
        except mysql.connector.Error as error:
            print("{}".format(error))
        except InvalidColumnValues:
            print("Column values are invalid. Dropping file.")
            continue
        except DuplicateValues:
            print("There is a problem. We are getting values with duplicate times and frequencies. Dropping file. PLEASE INFORM THE MAINTAINER OF THIS FILE SO WE CAN REPLICATE THE BUG.")
            continue
        
        for frequency_key,data_entry in formatted_RFI_file.get("Data").items():#for each value in that multi-valued set
            data_entry = manage_missing_cols(data_entry).getdata_entry()
            add_main_values = "INSERT INTO "+str(data_entry["Database"])+" (feed,frontend,`azimuth_deg`,projid,`resolution_MHz`,Window,exposure,utc_hrs,date,number_IF_Windows,Channel,backend,mjd,Frequency_MHz,lst,filename,polarization,source,tsys,frequency_type,units,Intensity_Jy,scan_number,`elevation_deg`, `Counts`) VALUES (\""+str(formatted_RFI_file.get("feed"))+"\",\""+str(formatted_RFI_file.get("frontend"))+"\",\""+str(formatted_RFI_file.get("azimuth (deg)"))+"\",\""+str(formatted_RFI_file.get("projid"))+"\",\""+str(formatted_RFI_file.get("frequency_resolution (MHz)"))+"\",\""+str(data_entry["Window"])+"\",\""+str(formatted_RFI_file.get("exposure (sec)"))+"\",\""+str(formatted_RFI_file.get("utc (hrs)"))+"\",\""+str(formatted_RFI_file.get("date"))+"\",\""+str(formatted_RFI_file.get("number_IF_Windows"))+"\",\""+str(data_entry["Channel"])+"\",\""+str(formatted_RFI_file.get("backend"))+"\",\""+str(formatted_RFI_file.get("mjd"))+"\",\""+str(frequency_key)+"\",\""+str(formatted_RFI_file.get("lst (hrs)"))+"\",\""+str(formatted_RFI_file.get("filename"))+"\",\""+str(formatted_RFI_file.get("polarization"))+"\",\""+str(formatted_RFI_file.get("source"))+"\",\""+str(formatted_RFI_file.get("tsys"))+"\",\""+str(formatted_RFI_file.get("frequency_type"))+"\",\""+str(formatted_RFI_file.get("units"))+"\",\""+str(data_entry["Intensity_Jy"])+"\",\""+str(formatted_RFI_file.get("scan_number"))+"\",\""+str(formatted_RFI_file.get("elevation (deg)"))+"\",\""+str(data_entry["Counts"])+"\");"
            cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
            cursor = cnx.cursor(buffered=True)
            try:
                cursor.execute(add_main_values)
                cnx.commit()
                cursor.close()
                duplicate_entry = False
            # If we find a duplicate entry, we will up the counts and average the intensities
            except mysql.connector.errors.IntegrityError:
                if (cnx.is_connected()):
                    cnx.close()
                cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                cursor = cnx.cursor(buffered=True)
                intensity_query = ("SELECT Intensity_Jy,filename,Counts from "+str(data_entry["Database"])+" WHERE Frequency_MHz = "+str(frequency_key)+" AND mjd = "+str(formatted_RFI_file.get("mjd")))
                cursor.execute(intensity_query) 
                responses = cursor.fetchall()
                for response in responses:
                    # Need to weight the 
                    current_counts = response[2]
                    old_intensity = float(response[0])
                    new_intensity = float(data_entry["Intensity_Jy"])
                    intensity_avg = (new_intensity+(old_intensity*float(current_counts)))/2.0
                    cursor.close()
                    old_filename = response[1]
                    if old_filename != "Duplicate":
                        cnx = connector.connect(user=username, password=password,
                            host=IP_address,
                            database=database)
                        cursor = cnx.cursor(buffered=True)
                        insert_old_duplicate_data = ("INSERT INTO duplicate_data_catalog (Frequency_MHz,Intensity_Jy,filename) VALUES (\'"+str(frequency_key)+"\',\'"+str(old_intensity)+"\',\'"+str(old_filename)+"\')")
                        cursor.execute(insert_old_duplicate_data)
                        cnx.commit()
                        cursor.close()
                    cnx = connector.connect(user=username, password=password,
                        host=IP_address,
                        database=database)
                    cursor = cnx.cursor(buffered=True)
                    update_avg_intensity = ("UPDATE "+str(data_entry["Database"]+" SET Counts = "+str(int(current_counts)+ 1)+", Intensity_Jy = "+str(intensity_avg)+", Window = \'NaN\', Channel = \'NaN\', filename = \'Duplicate\' where Frequency_MHz = "+str(frequency_key)+" AND mjd = "+str(formatted_RFI_file.get("mjd"))))
                    cursor.execute(update_avg_intensity)
                    cnx.commit()
                    cursor.close()
                    cnx = connector.connect(user=username, password=password,
                        host=IP_address,
                        database=database)
                    cursor = cnx.cursor(buffered=True)
                    insert_new_duplicate_data = ("INSERT INTO duplicate_data_catalog (Frequency_MHz,Intensity_Jy,filename) VALUES (\'"+str(frequency_key)+"\',\'"+str(new_intensity)+"\',\'"+str(formatted_RFI_file.get("filename"))+"\')")
                    cursor.execute(insert_new_duplicate_data)
                    cnx.commit()
                    cursor.close()
                    
                duplicate_entry = True
            
            # We have some receiver names that are too generic or specific for our receiver tables, so we're making that consistent
            frontend_for_rcvr_table = rfitrends.GBT_receiver_specs.PrepareFrontendInput(formatted_RFI_file.get("frontend"))
            # Putting composite key values into the receiver table
            if frontend_for_rcvr_table != 'Unknown' and not duplicate_entry:
                add_receiver_keys = "INSERT INTO "+frontend_for_rcvr_table+" (Frequency_MHz,mjd) VALUES (\""+str(frequency_key)+"\", \""+str(formatted_RFI_file.get("mjd"))+"\");"
                cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                cursor = cnx.cursor(buffered=True)
                cursor.execute(add_receiver_keys)
                cnx.commit()
                cursor.close()
                cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                cursor = cnx.cursor(buffered=True)
                cursor.execute("SELECT projid,mjd from latest_projects WHERE frontend= \""+frontend_for_rcvr_table+"\"")
                cnx.commit()
                rows = cursor.fetchall()
                for row in rows: 
                    latest_projid = row[0]
                    latest_mjd  = row[1]
                cursor.close()
                if latest_mjd < float(formatted_RFI_file.get("mjd")) and (formatted_RFI_file.get("projid") != 'NaN'):
                    # Before we replace the previous latest project with the current one, we want to drop the table containing the previous latest projects' data:
                    if latest_projid != "None":
                        cnx = connector.connect(user=username, password=password,
                        host=IP_address,
                        database=database)
                        cursor = cnx.cursor(buffered=True)
                        cursor.execute("DROP table "+latest_projid)
                        cnx.commit()
                        cursor.close()
                    # Now we can update the project id:
                    update_latest_projid = "UPDATE latest_projects SET projid=\""+str(formatted_RFI_file.get("projid"))+"\" WHERE frontend = \""+frontend_for_rcvr_table+"\";"
                    cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                    cursor = cnx.cursor(buffered=True)
                    cursor.execute(update_latest_projid)
                    cnx.commit()
                    cursor.close()
                    update_latest_date = "UPDATE latest_projects SET mjd=\""+str(formatted_RFI_file.get("mjd"))+"\" WHERE frontend = \""+frontend_for_rcvr_table+"\";"
                    cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                    cursor = cnx.cursor(buffered=True)
                    cursor.execute(update_latest_date)
                    cnx.commit()
                    cursor.close()
                    # The new latest project is the most recent project we just updated
                    latest_projid = str(formatted_RFI_file.get("projid"))
                if formatted_RFI_file.get("projid") == latest_projid and (formatted_RFI_file.get("projid") != 'NaN'):
                    projid_table_maker = "CREATE TABLE IF NOT EXISTS "+latest_projid+" (Frequency_MHz Decimal(12,6), mjd Decimal(8,3), PRIMARY KEY (Frequency_MHz,mjd));"
                    cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                    cursor = cnx.cursor(buffered=True)
                    cursor.execute(projid_table_maker)
                    cnx.commit()
                    cursor.close()
                    projid_populate_table = "INSERT INTO "+formatted_RFI_file.get("projid")+" (Frequency_MHz,mjd) VALUES (\""+str(frequency_key)+"\", \""+str(formatted_RFI_file.get("mjd"))+"\");"
                    cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
                    cursor = cnx.cursor(buffered=True)
                    cursor.execute(projid_populate_table)
                    cnx.commit()
                    cursor.close()
        """
        # Now that we've uploaded everything we want from the file, we want to delete any outdated tables that are no longer one of the latest projects           
        cnx = connector.connect(user=username, password=password,
                    host=IP_address,
                    database=database)
        cursor = cnx.cursor(buffered=True)
        # Getting the table names
        cursor.execute("SHOW TABLES")
        table_values = []
        for (table_name,) in cursor:
            table_values.append(table_name)
        cnx.commit()
        cursor.close()
        # Going through each table name one by one:
        for table_name in table_values:
            cnx = connector.connect(user=username, password=password,
                        host=IP_address,
                        database=database)
            cursor = cnx.cursor(buffered=True)
            # Check if the table name exists in the latest_projects table. If not, we should remove it.
            query = "SELECT EXISTS(SELECT * from latest_projects where projid = \""+table_name+"\")"
            cursor.execute(query)
            cnx.commit()
            exists = cursor.fetchall()[0][0]
            cursor.close()
            # Remove table if it is one of the redundant ones. Have to check if it starts with TRFI, otherwise it'd delete our main table and Django tables
            if exists == 0 and table_name.startswith("TRFI"):
                cnx = connector.connect(user=username, password=password,
                        host=IP_address,
                        database=database)
                cursor = cnx.cursor(buffered=True)
                delete_extra_table = "DROP table "+table_name
                cursor.execute(delete_extra_table)
                cnx.commit()
                cursor.close()
        """


        print(str(filename)+" uploaded.")

            
    print("All files uploaded.")

        


    cnx.close()

if __name__ == "__main__":
    import ptvsd 
    # Allow other computers to attach to ptvsd at this IP address and port. 
    ptvsd.enable_attach(address=('10.16.96.210', 3001), redirect_output=True) 
    ptvsd.wait_for_attach()
    # Adding in-line arguments:
    parser = argparse.ArgumentParser(description="Takes .txt files of RFI data and uploads them to the given database")
    parser.add_argument("main_table",help="The string name of the table to which you'd like to upload your clean RFI data")
    parser.add_argument("dirty_table",help="The string name of the table to which you'd like to upload your flagged or bad RFI data")
    parser.add_argument("path",help="The path to the .txt files that need to be uploaded to the database")
    parser.add_argument("IP_address",nargs='?',default= '192.33.116.22',help="The IP address to find the SQL database to which you would like to add this table. Default is the GBO development server address. This would only work for employees.")
    parser.add_argument("database",nargs='?',default='jskipper',help="The name of the SQL database to which you would like to add this table. Default is jskipper, which would only work for employees.")
    args = parser.parse_args()
    main_table = args.main_table
    dirty_table = args.dirty_table
    IP_address = args.IP_address
    database = args.database
    # The likely path to use for filepath_to_rfi_scans if looking at most recent (last 6 months) of RFI data for GBT:
    #path = '/home/www.gb.nrao.edu/content/IPG/rfiarchive_files/GBTDataImages'
    path = args.path   
    username, password = prompt_user_login_to_database(IP_address,database)
    config = configparser.ConfigParser()
    write_to_database(username, password, IP_address, database, main_table,dirty_table,path)


        
    


