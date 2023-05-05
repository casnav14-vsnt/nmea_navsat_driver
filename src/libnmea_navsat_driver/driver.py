# Software License Agreement (BSD License)
#
# Copyright (c) 2013, Eric Perko
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the names of the authors nor the names of their
#    affiliated organizations may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Provides a driver for NMEA GNSS devices."""

import math

import rospy

from sensor_msgs.msg import NavSatFix, NavSatStatus, TimeReference
from geometry_msgs.msg import TwistStamped, QuaternionStamped
from tf.transformations import quaternion_from_euler
from mav_msgs.msg import Actuators
from std_msgs.msg import String
from rospy_tutorials.msg import HeaderString

from libnmea_navsat_driver.checksum_utils import check_nmea_checksum
import libnmea_navsat_driver.parser
from nmea_navsat_driver.msg import *

# from datetime import datetime
# import os

# try:
#     os.mkdir('/home/vsnt/logs/')
# except:
#     pass

# now = datetime.now()
# dt_string = now.strftime("%d-%m-%Y %H:%M:%S")

# f = open('/home/vsnt/logs/'+dt_string, 'w')

class RosNMEADriver(object):
    """ROS driver for NMEA GNSS devices."""

    def __init__(self):
        """Initialize the ROS NMEA driver.

        :ROS Publishers:
            - NavSatFix publisher on the 'fix' channel.
            - TwistStamped publisher on the 'vel' channel.
            - QuaternionStamped publisher on the 'heading' channel.
            - TimeReference publisher on the 'time_reference' channel.

        :ROS Parameters:
            - ~time_ref_source (str)
                The name of the source in published TimeReference messages. (default None)
            - ~useRMC (bool)
                If true, use RMC NMEA messages. If false, use GGA and VTG messages. (default False)
            - ~epe_quality0 (float)
                Value to use for default EPE quality for fix type 0. (default 1000000)
            - ~epe_quality1 (float)
                Value to use for default EPE quality for fix type 1. (default 4.0)
            - ~epe_quality2 (float)
                Value to use for default EPE quality for fix type 2. (default (0.1)
            - ~epe_quality4 (float)
                Value to use for default EPE quality for fix type 4. (default 0.02)
            - ~epe_quality5 (float)
                Value to use for default EPE quality for fix type 5. (default 4.0)
            - ~epe_quality9 (float)
                Value to use for default EPE quality for fix type 9. (default 3.0)
        """
        self.fix_pub = rospy.Publisher('fix', NavSatFix, queue_size=1)
        self.vel_pub = rospy.Publisher('vel', TwistStamped, queue_size=1)
        self.use_GNSS_time = rospy.get_param('~use_GNSS_time', False)
        self.rudder_pub = rospy.Publisher('rudder_angle', Actuators, queue_size=1)
        self.gsa_pub = rospy.Publisher('gsa', Gsa, queue_size=1)
        self.zda_pub = rospy.Publisher('zda', Timedate, queue_size=1)
        self.rpm_pub = rospy.Publisher('rpm', Engine, queue_size=1)
        self.hdg_pub = rospy.Publisher('hdg', Magnetic, queue_size=1)
        self.rot_pub = rospy.Publisher('rot', Rateofturn, queue_size=1)
        self.gsv_pub = rospy.Publisher('gsv', Gsv, queue_size=1)
        self.vtg_pub = rospy.Publisher('vtg', Trackmadegood, queue_size=1)
        self.vbw_pub = rospy.Publisher('vbw', Relativespeeds, queue_size=1)
        self.gll_pub = rospy.Publisher('gll', Gll, queue_size=1)
        self.vdm_pub = rospy.Publisher('vdm', Vd, queue_size=1)
        self.vdo_pub = rospy.Publisher('vdo', Vd, queue_size=1)
        self.hdt_pub = rospy.Publisher('hdt', Hdt, queue_size=1)
        if not self.use_GNSS_time:
            self.time_ref_pub = rospy.Publisher(
                'time_reference', TimeReference, queue_size=1)
            
        self.nmea_pub = rospy.Publisher('nmea_sentences', HeaderString, queue_size=10)

        self.time_ref_source = rospy.get_param('~time_ref_source', None)
        self.use_RMC = rospy.get_param('~useRMC', False)
        self.valid_fix = False

        # epe = estimated position error
        self.default_epe_quality0 = rospy.get_param('~epe_quality0', 1000000)
        self.default_epe_quality1 = rospy.get_param('~epe_quality1', 4.0)
        self.default_epe_quality2 = rospy.get_param('~epe_quality2', 0.1)
        self.default_epe_quality4 = rospy.get_param('~epe_quality4', 0.02)
        self.default_epe_quality5 = rospy.get_param('~epe_quality5', 4.0)
        self.default_epe_quality9 = rospy.get_param('~epe_quality9', 3.0)
        self.using_receiver_epe = False

        self.lon_std_dev = float("nan")
        self.lat_std_dev = float("nan")
        self.alt_std_dev = float("nan")

        """Format for this dictionary is the fix type from a GGA message as the key, with
        each entry containing a tuple consisting of a default estimated
        position error, a NavSatStatus value, and a NavSatFix covariance value."""
        self.gps_qualities = {
            # Unknown
            -1: [
                self.default_epe_quality0,
                NavSatStatus.STATUS_NO_FIX,
                NavSatFix.COVARIANCE_TYPE_UNKNOWN
            ],
            # Invalid
            0: [
                self.default_epe_quality0,
                NavSatStatus.STATUS_NO_FIX,
                NavSatFix.COVARIANCE_TYPE_UNKNOWN
            ],
            # SPS
            1: [
                self.default_epe_quality1,
                NavSatStatus.STATUS_FIX,
                NavSatFix.COVARIANCE_TYPE_APPROXIMATED
            ],
            # DGPS
            2: [
                self.default_epe_quality2,
                NavSatStatus.STATUS_SBAS_FIX,
                NavSatFix.COVARIANCE_TYPE_APPROXIMATED
            ],
            # RTK Fix
            4: [
                self.default_epe_quality4,
                NavSatStatus.STATUS_GBAS_FIX,
                NavSatFix.COVARIANCE_TYPE_APPROXIMATED
            ],
            # RTK Float
            5: [
                self.default_epe_quality5,
                NavSatStatus.STATUS_GBAS_FIX,
                NavSatFix.COVARIANCE_TYPE_APPROXIMATED
            ],
            # WAAS
            9: [
                self.default_epe_quality9,
                NavSatStatus.STATUS_GBAS_FIX,
                NavSatFix.COVARIANCE_TYPE_APPROXIMATED
            ]
        }

    def add_sentence(self, nmea_string, frame_id, timestamp=None):
        """Public method to provide a new NMEA sentence to the driver.

        Args:
            nmea_string (str): NMEA sentence in string form.
            frame_id (str): TF frame ID of the GPS receiver.
            timestamp(rospy.Time, optional): Time the sentence was received.
                If timestamp is not specified, the current time is used.

        Returns:
            bool: True if the NMEA string is successfully processed, False if there is an error.
        """

        try:
            nmea_raw_splitted = nmea_string.split("\\")
            for sentence in nmea_raw_splitted:
                try:
                    if sentence[0] == '$' or sentence[0] == '!':
                        nmea_str = sentence
                        # f.write(nmea_str + "\r\n")
                        if nmea_str[0:6] != "$MXPGN" and nmea_str[0:9] != "$PSMDSTAT" and nmea_str[0:6] != "$AGRSA" and nmea_str[0:6] != "$ERRPM":
                            pub_data = HeaderString()
                            pub_data.header.stamp = rospy.get_rostime()
                            pub_data.data = nmea_str + "\r\n"
                            print(pub_data.data)
                            self.nmea_pub.publish(pub_data)
                except:
                    pass
        except:
            pass
        
        if not check_nmea_checksum(nmea_string):
            rospy.logwarn("Received a sentence with an invalid checksum. " +
                          "Sentence was: %s" % repr(nmea_string))
            return False

        parsed_sentence = libnmea_navsat_driver.parser.parse_nmea_sentence(
            nmea_string)
        if not parsed_sentence:
            rospy.logdebug(
                "Failed to parse NMEA sentence. Sentence was: %s" %
                nmea_string)
            return False

        if timestamp:
            current_time = timestamp
        else:
            current_time = rospy.get_rostime()
        current_fix = NavSatFix()
        current_fix.header.stamp = current_time
        current_fix.header.frame_id = frame_id
        if not self.use_GNSS_time:
            current_time_ref = TimeReference()
            current_time_ref.header.stamp = current_time
            current_time_ref.header.frame_id = frame_id
            if self.time_ref_source:
                current_time_ref.source = self.time_ref_source
            else:
                current_time_ref.source = frame_id

        if not self.use_RMC and 'GGA' in parsed_sentence:
            current_fix.position_covariance_type = \
                NavSatFix.COVARIANCE_TYPE_APPROXIMATED

            data = parsed_sentence['GGA']

            if self.use_GNSS_time:
                if math.isnan(data['utc_time'][0]):
                    rospy.logwarn("Time in the NMEA sentence is NOT valid")
                    return False
                current_fix.header.stamp = rospy.Time(data['utc_time'][0], data['utc_time'][1])

            fix_type = data['fix_type']
            if not (fix_type in self.gps_qualities):
                fix_type = -1
            gps_qual = self.gps_qualities[fix_type]
            default_epe = gps_qual[0]
            current_fix.status.status = gps_qual[1]
            current_fix.position_covariance_type = gps_qual[2]

            self.valid_fix = (fix_type > 0)

            current_fix.status.service = NavSatStatus.SERVICE_GPS

            latitude = data['latitude']
            if data['latitude_direction'] == 'S':
                latitude = -latitude
            current_fix.latitude = latitude

            longitude = data['longitude']
            if data['longitude_direction'] == 'W':
                longitude = -longitude
            current_fix.longitude = longitude

            # Altitude is above ellipsoid, so adjust for mean-sea-level
            altitude = data['altitude'] + data['mean_sea_level']
            current_fix.altitude = altitude

            # use default epe std_dev unless we've received a GST sentence with
            # epes
            if not self.using_receiver_epe or math.isnan(self.lon_std_dev):
                self.lon_std_dev = default_epe
            if not self.using_receiver_epe or math.isnan(self.lat_std_dev):
                self.lat_std_dev = default_epe
            if not self.using_receiver_epe or math.isnan(self.alt_std_dev):
                self.alt_std_dev = default_epe * 2

            hdop = data['hdop']
            current_fix.position_covariance[0] = (hdop * self.lon_std_dev) ** 2
            current_fix.position_covariance[4] = (hdop * self.lat_std_dev) ** 2
            current_fix.position_covariance[8] = (
                2 * hdop * self.alt_std_dev) ** 2  # FIXME

            self.fix_pub.publish(current_fix)

            if not (math.isnan(data['utc_time'][0]) or self.use_GNSS_time):
                current_time_ref.time_ref = rospy.Time(
                    data['utc_time'][0], data['utc_time'][1])
                self.last_valid_fix_time = current_time_ref
                self.time_ref_pub.publish(current_time_ref)


        elif 'RMC' in parsed_sentence:
            data = parsed_sentence['RMC']

            if self.use_GNSS_time:
                if math.isnan(data['utc_time'][0]):
                    rospy.logwarn("Time in the NMEA sentence is NOT valid")
                    return False
                current_fix.header.stamp = rospy.Time(data['utc_time'][0], data['utc_time'][1])

            # Only publish a fix from RMC if the use_RMC flag is set.
            if self.use_RMC:
                if data['fix_valid']:
                    current_fix.status.status = NavSatStatus.STATUS_FIX
                else:
                    current_fix.status.status = NavSatStatus.STATUS_NO_FIX

                current_fix.status.service = NavSatStatus.SERVICE_GPS

                latitude = data['latitude']
                if data['latitude_direction'] == 'S':
                    latitude = -latitude
                current_fix.latitude = latitude

                longitude = data['longitude']
                if data['longitude_direction'] == 'W':
                    longitude = -longitude
                current_fix.longitude = longitude

                current_fix.altitude = float('NaN')
                current_fix.position_covariance_type = \
                    NavSatFix.COVARIANCE_TYPE_UNKNOWN

                self.fix_pub.publish(current_fix)

                if not (math.isnan(data['utc_time'][0]) or self.use_GNSS_time):
                    current_time_ref.time_ref = rospy.Time(
                        data['utc_time'][0], data['utc_time'][1])
                    self.time_ref_pub.publish(current_time_ref)

            # Publish velocity from RMC regardless, since GGA doesn't provide
            # it.
            if data['fix_valid']:
                current_vel = TwistStamped()
                current_vel.header.stamp = current_time
                current_vel.header.frame_id = frame_id
                current_vel.twist.linear.x = data['speed'] * \
                    math.sin(data['true_course'])
                current_vel.twist.linear.y = data['speed'] * \
                    math.cos(data['true_course'])
                self.vel_pub.publish(current_vel)
        elif 'GST' in parsed_sentence:
            data = parsed_sentence['GST']

            # Use receiver-provided error estimate if available
            self.using_receiver_epe = True
            self.lon_std_dev = data['lon_std_dev']
            self.lat_std_dev = data['lat_std_dev']
            self.alt_std_dev = data['alt_std_dev']
        elif 'HDT' in parsed_sentence:
            data = parsed_sentence['HDT']
            
            hdt = Hdt()
            hdt.header.stamp = current_time
            hdt.header.frame_id = frame_id
            hdt.heading = data['heading']
            hdt.heading_relative = data['heading_relative']

            self.hdt_pub.publish(hdt)

        elif 'RSA' in parsed_sentence:
            data = parsed_sentence['RSA']
            if data['rudder_angle']:
                current_rudder_angle = Actuators()
                current_rudder_angle.header.stamp = current_time
                current_rudder_angle.header.frame_id = frame_id
                current_rudder_angle.angles.append(data['rudder_angle'])
                self.rudder_pub.publish(current_rudder_angle)

        elif 'GSA' in parsed_sentence:
            data = parsed_sentence['GSA']
            
            gsa = Gsa()
            gsa.mode = data['mode_one']
            gsa.fix_type = data['fix_type_']
            gsa.sats.sat1 = data['prn_number_sat1']
            gsa.sats.sat2 = data['prn_number_sat2']
            gsa.sats.sat3 = data['prn_number_sat3']
            gsa.sats.sat4 = data['prn_number_sat4']
            gsa.sats.sat5 = data['prn_number_sat5']
            gsa.sats.sat6 = data['prn_number_sat6']
            gsa.sats.sat7 = data['prn_number_sat7']
            gsa.sats.sat8 = data['prn_number_sat8']
            gsa.sats.sat9 = data['prn_number_sat9']
            gsa.sats.sat10 = data['prn_number_sat10']
            gsa.sats.sat11 = data['prn_number_sat11']
            gsa.sats.sat12 = data['prn_number_sat12']
            gsa.satfix.header.stamp = current_time
            gsa.satfix.header.frame_id = frame_id
            gsa.header.stamp = current_time
            gsa.header.frame_id = frame_id
            gsa.satfix.latitude = data['pdop']
            gsa.satfix.longitude = data['hdop']
            gsa.satfix.altitude = data['vdop']
    
            self.gsa_pub.publish(gsa)

        elif 'ZDA' in parsed_sentence:
            data = parsed_sentence['ZDA']

            zda = Timedate()
            zda.header.stamp = current_time
            zda.header.frame_id = frame_id
            zda.utc = data['utc']
            zda.day = data['day']
            zda.month = data['month']
            zda.year = data['year']

            self.zda_pub.publish(zda)

        elif 'RPM' in parsed_sentence:
            data = parsed_sentence['RPM']

            rpm = Engine()
            rpm.header.stamp = current_time
            rpm.header.frame_id = frame_id
            rpm.engine_status = data['engine_status']
            rpm.rpm = data['rpm']
            rpm.engine_hours = data['engine_hours']
            rpm.propeller_pitch = data['propeller_pitch']

            self.rpm_pub.publish(rpm)

        elif 'HDG' in parsed_sentence:
            data = parsed_sentence['HDG']

            hdg = Magnetic()
            hdg.header.stamp = current_time
            hdg.header.frame_id = frame_id
            hdg.magnetic_sensor_heading = data['magnetic_sensor_heading']
            hdg.magnetic_deviation = data['magnetic_deviation']
            hdg.magnetic_deviation_direction = data['magnetic_deviation_direction']
            hdg.magnetic_variation_degrees = data['magnetic_variation_degrees']

            self.hdg_pub.publish(hdg)

        elif 'ROT' in parsed_sentence:
            data = parsed_sentence['ROT']

            rot= Rateofturn()
            rot.header.stamp = current_time
            rot.header.frame_id = frame_id
            rot.rate_of_turn = data['rate_of_turn']

            self.rot_pub.publish(rot)

        elif 'GSV' in parsed_sentence:
            data = parsed_sentence['GSV']

            gsv = Gsv()
            gsv.header.stamp = current_time
            gsv.header.frame_id = frame_id
            gsv.total_gsv_msgs_in_this_cycle = data['total_gsv_msgs_in_this_cycle']
            gsv.message_number = data['message_number']
            gsv.total_number_of_SVs_visible = data['total_number_of_SVs_visible']
            gsv.sv1.header.stamp = current_time
            gsv.sv1.header.frame_id = frame_id
            gsv.sv1.prn_number = data['SV1_PRN_number']
            gsv.sv1.elevation = data['SV1_elevation']
            gsv.sv1.azimuth = data['SV1_azimuth']
            gsv.sv1.snr = data['SV1_SNR']
            gsv.sv2.header.stamp = current_time
            gsv.sv2.header.frame_id = frame_id
            gsv.sv2.prn_number = data['SV2_PRN_number']
            gsv.sv2.elevation = data['SV2_elevation']
            gsv.sv2.azimuth = data['SV2_azimuth']
            gsv.sv2.snr = data['SV2_SNR']
            gsv.sv3.header.stamp = current_time
            gsv.sv3.header.frame_id = frame_id
            gsv.sv3.prn_number = data['SV3_PRN_number']
            gsv.sv3.elevation = data['SV3_elevation']
            gsv.sv3.azimuth = data['SV3_azimuth']
            gsv.sv3.snr = data['SV3_SNR']
            gsv.sv4.header.stamp = current_time
            gsv.sv4.header.frame_id = frame_id
            gsv.sv4.prn_number = data['SV4_PRN_number']
            gsv.sv4.elevation = data['SV4_elevation']
            gsv.sv4.azimuth = data['SV4_azimuth']
            gsv.sv4.snr = data['SV4_SNR']

            self.gsv_pub.publish(gsv)

        elif 'VTG' in parsed_sentence:
            data = parsed_sentence['VTG']

            vtg = Trackmadegood()
            vtg.header.stamp = current_time
            vtg.header.frame_id = frame_id
            vtg.track_made_good_degrees_true = data['track_made_good_degrees_true']
            vtg.track_made_good_degrees_magnetic = data['track_made_good_degrees_magnetic']
            vtg.speed_in_knots = data['speed_in_knots']
            vtg.speed_in_kph = data['speed_in_kph']
            vtg.mode_indicator = data['mode_indicator']

            self.vtg_pub.publish(vtg)

        elif 'VBW' in parsed_sentence:
            data = parsed_sentence['VBW']

            vbw = Relativespeeds()
            vbw.header.stamp = current_time
            vbw.header.frame_id = frame_id
            vbw.water_speed_longitudinal_component = data['water_speed_longitudinal_component']
            vbw.water_speed_transverse_component = data['water_speed_transverse_component']
            vbw.water_speed_status_data = data['water_speed_status_data']
            vbw.over_ground_velocity_longitudinal_component = data['over_ground_velocity_longitudinal_component']
            vbw.over_ground_velocity_transverse_component = data['over_ground_velocity_transverse_component']
            vbw.over_ground_velocity_status_data = data['over_ground_velocity_status_data']
            vbw.stern_transverse_water_speed = data['stern_transverse_water_speed']
            vbw.stern_transverse_water_speed_status_data= data['stern_transverse_water_speed_status_data']
            vbw.stern_transverse_ground_speed = data['stern_transverse_ground_speed']
            vbw.stern_transverse_ground_speed_status_data = data['stern_transverse_ground_speed_status_data']

            self.vbw_pub.publish(vbw)

        elif 'GLL' in parsed_sentence:
            data = parsed_sentence['GLL']

            gll = Gll()
            gll.header.stamp = current_time
            gll.header.frame_id = frame_id
            gll.position.latitude = data['latitude']
            gll.position.longitude = data['longitude']
            gll.latitude_direction = data['latitude_direction']
            gll.longitude_direction = data['longitude_direction']
            gll.position.header.stamp = current_time
            gll.position.header.frame_id = frame_id
            gll.utc.utc = data['utc']
            gll.utc.header.stamp = current_time
            gll.utc.header.frame_id = frame_id
            gll.data_status = data['data_status']
            gll.mode_indicator = data['mode_indicator']

            self.gll_pub.publish(gll)

        elif 'VDM' in parsed_sentence:
            data = parsed_sentence['VDM']

            vdm = Vd()
            vdm.header.stamp = current_time
            vdm.header.frame_id = frame_id
            vdm.fragments_in_currently_accumulating_message = data['fragments_in_currently_accumulating_message']
            vdm.fragment_number = data['fragment_number']
            vdm.sequential_message_id = data['sequential_message_id']
            vdm.radio_channel_code = data['radio_channel_code']
            vdm.data_payload = data['data_payload']
            vdm.fill_bits = data['fill_bits']

            self.vdm_pub.publish(vdm)

        elif 'VDO' in parsed_sentence:
            data = parsed_sentence['VDO']

            vdo = Vd()
            vdo.header.stamp = current_time
            vdo.header.frame_id = frame_id
            vdo.fragments_in_currently_accumulating_message = data['fragments_in_currently_accumulating_message']
            vdo.fragment_number = data['fragment_number']
            vdo.sequential_message_id = data['sequential_message_id']
            vdo.radio_channel_code = data['radio_channel_code']
            vdo.data_payload = data['data_payload']
            vdo.fill_bits = data['fill_bits']

            self.vdo_pub.publish(vdo)

        else:
            return False

    @staticmethod
    def get_frame_id():
        """Get the TF frame_id.

        Queries rosparam for the ~frame_id param. If a tf_prefix param is set,
        the frame_id is prefixed with the prefix.

        Returns:
            str: The fully-qualified TF frame ID.
        """
        frame_id = rospy.get_param('~frame_id', 'gps')
        # Add the TF prefix
        prefix = ""
        prefix_param = rospy.search_param('tf_prefix')
        if prefix_param:
            prefix = rospy.get_param(prefix_param)
            return "%s/%s" % (prefix, frame_id)
        else:
            return frame_id
