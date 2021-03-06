# validated: 2017-11-21 EN 34c18ef00062 edu/wpi/first/wpilibj/GyroBase.java
#----------------------------------------------------------------------------
# Copyright (c) FIRST 2008-2012. All Rights Reserved.
# Open Source Software - may be modified and shared by FRC teams. The code
# must be accompanied by the FIRST BSD license file in the root directory of
# the project.
#----------------------------------------------------------------------------


from .interfaces import PIDSource
from .sensorbase import SensorBase

__all__ = ['GyroBase']

class GyroBase(SensorBase):
    '''
        GyroBase is the common base class for Gyro implementations such as
        :class:`.AnalogGyro`.
    '''
    
    PIDSourceType = PIDSource.PIDSourceType
    
    def __init__(self):
        self.pidSource = self.PIDSourceType.kDisplacement
        self.valueEntry = None
    
    def calibrate(self):
        raise NotImplementedError()
    
    def reset(self):
        raise NotImplementedError()
    
    def getAngle(self):
        raise NotImplementedError()
    
    def getRate(self):
        raise NotImplementedError()
    
    def setPIDSourceType(self, pidSource):
        """Set which parameter of the gyro you are using as a process
        control variable. The Gyro class supports the rate and angle
        parameters.

        :param pidSource: An enum to select the parameter.
        :type  pidSource: :class:`.PIDSource.PIDSourceType`
        """
        if pidSource not in (self.PIDSourceType.kDisplacement,
                             self.PIDSourceType.kRate):
            raise ValueError("Must be kRate or kDisplacement")
        self.pidSource = pidSource
        
    def getPIDSourceType(self):
        return self.pidSource
    
    def pidGet(self):
        """Get the output of the gyro for use with PIDControllers. May be
        the angle or rate depending on the set :class:`.PIDSourceType`

        :returns: the current angle according to the gyro
        :rtype: float
        """
        if self.pidSource == self.PIDSourceType.kRate:
            return self.getRate()
        elif self.pidSource == self.PIDSourceType.kDisplacement:
            return self.getAngle()
        else:
            return 0.0
    
    # Live Window code, only does anything if live window is activated.

    def getSmartDashboardType(self):
        return "Gyro"

    def initTable(self, subtable):
        if subtable is not None:
            self.valueEntry = subtable.getEntry("Value")
            self.updateTable()
        else:
            self.valueEntry = None
        
    def updateTable(self):
        if self.valueEntry is not None:
            self.valueEntry.setDouble(self.getAngle())

    def startLiveWindowMode(self):
        pass

    def stopLiveWindowMode(self):
        pass
    
    
    
