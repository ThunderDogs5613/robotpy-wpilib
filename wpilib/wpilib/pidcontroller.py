# validated: 2017-11-19 EN 14fcf3f2f0d9 edu/wpi/first/wpilibj/PIDController.java
#----------------------------------------------------------------------------
# Copyright (c) FIRST 2008-2016. All Rights Reserved.
# Open Source Software - may be modified and shared by FRC teams. The code
# must be accompanied by the FIRST BSD license file in the root directory of
# the project.
#----------------------------------------------------------------------------

from collections import deque
from itertools import islice
import threading
import warnings
from networktables import NetworkTables

import hal

from .interfaces import PIDSource
from .livewindowsendable import LiveWindowSendable
from .resource import Resource
from .timer import Timer
from ._impl.timertask import TimerTask
from ._impl.utils import match_arglist, HasAttribute

__all__ = ["PIDController"]

class PIDController(LiveWindowSendable):
    """Can be used to control devices via a PID Control Loop.

    Creates a separate thread which reads the given :class:`.PIDSource` and takes
    care of the integral calculations, as well as writing the given
    :class:`.PIDOutput`.

    This feedback controller runs in discrete time, so time deltas are not used
    in the integral and derivative calculations. Therefore, the sample rate affects
    the controller's behavior for a given set of PID constants.
    """
    kDefaultPeriod = .05
    instances = 0
    
    PIDSourceType = PIDSource.PIDSourceType

    # Tolerance is the type of tolerance used to specify if the PID controller
    # is on target.  The various implementations of this such as
    # PercentageTolerance and AbsoluteTolerance specify types of tolerance
    # specifications to use.
    def PercentageTolerance_onTarget(self, percentage):
        with self.mutex:
            return self.isAvgErrorValid() and \
                    (abs(self.getAvgError()) < percentage / 100.0
                    * (self.maximumInput - self.minimumInput))

    def AbsoluteTolerance_onTarget(self, value):
        return self.isAvgErrorValid() and abs(self.getAvgError()) < value

    def __init__(self, Kp, Ki, Kd, *args, **kwargs):
        """Allocate a PID object with the given constants for P, I, D, and F

        Arguments can be structured as follows:

        - Kp, Ki, Kd, Kf, source, output, period
        - Kp, Ki, Kd, source, output, period
        - Kp, Ki, Kd, source, output
        - Kp, Ki, Kd, Kf, source, output

        :param Kp: the proportional coefficient
        :type  Kp: float or int
        :param Ki: the integral coefficient
        :type  Ki: float or int
        :param Kd: the derivative coefficient
        :type  Kd: float or int
        :param Kf: the feed forward term
        :type  Kf: float or int
        :param source: Called to get values
        :type  source: A function, or an object that implements :class:`.PIDSource`
        :param output: Receives the output percentage
        :type  output: A function, or an object that implements :class:`.PIDOutput`
        :param period: the loop time for doing calculations. This particularly
            effects calculations of the integral and differential terms.
            The default is 50ms.
        :type  period: float or int
        """

        f_arg = ("Kf", [float, int])
        source_arg = ("source", [HasAttribute("pidGet"), HasAttribute("__call__")])
        output_arg = ("output", [HasAttribute("pidWrite"), HasAttribute("__call__")])
        period_arg = ("period", [float, int])

        templates = [[f_arg, source_arg, output_arg, period_arg],
                     [source_arg, output_arg, period_arg],
                     [source_arg, output_arg],
                     [f_arg, source_arg, output_arg]]

        _, results = match_arglist('PIDController.__init__',
                                   args, kwargs, templates)

        self.P = Kp  # factor for "proportional" control
        self.I = Ki  # factor for "integral" control
        self.D = Kd  # factor for "derivative" control
        self.F = results.pop("Kf", 0.0)  # factor for feedforward term
        self.pidOutput = results.pop("output")
        self.pidInput = results.pop("source")
        self.period = results.pop("period", self.kDefaultPeriod)
        
        self.pidInput = PIDSource.from_obj_or_callable(self.pidInput)
        
        if hasattr(self.pidOutput, 'pidWrite'):
            self.pidOutput = self.pidOutput.pidWrite

        self.maximumOutput = 1.0    # |maximum output|
        self.minimumOutput = -1.0   # |minimum output|
        self.maximumInput = 0.0     # maximum input - limit setpoint to this
        self.minimumInput = 0.0     # minimum input - limit setpoint to this
        self.continuous = False     # do the endpoints wrap around? eg. Absolute encoder
        self.enabled = False        # is the pid controller enabled
        self.prevError = 0.0        # the prior error (used to compute velocity)
        self.totalError = 0.0       #the sum of the errors for use in the integral calc
        self.buf = deque(maxlen=1)
        self.setpoint = 0.0
        self.prevSetpoint = 0.0
        self.error = 0.0
        self.result = 0.0

        self.mutex = threading.RLock()
        self.pEntry = None
        self.iEntry = None
        self.dEntry = None
        self.fEntry = None
        self.setpointEntry = None
        self.enabledEntry = None

        self.pid_task = TimerTask('PIDTask%d' % PIDController.instances, self.period, self._calculate)
        self.pid_task.start()
        
        self.setpointTimer = Timer()
        self.setpointTimer.start()
        
        # Need this to free on unit test wpilib reset
        Resource._add_global_resource(self)

        PIDController.instances += 1
        hal.report(hal.UsageReporting.kResourceType_PIDController,
                   PIDController.instances)

    def free(self):
        """Free the PID object"""
        # TODO: is this useful in Python?  Should make TableListener weakref.
        self.pid_task.cancel()
        with self.mutex:
            self.pidInput = None
            self.pidOutput = None
        self.removeListeners()

    def _calculate(self):
        """Read the input, calculate the output accordingly, and write to the
        output.  This should only be called by the PIDTask and is created
        during initialization."""
        with self.mutex:
            if self.pidInput is None:
                return
            if self.pidOutput is None:
                return
            enabled = self.enabled # take snapshot of these values...
            pidInput = self.pidInput

        if enabled:
            with self.mutex:
                input = pidInput.pidGet()
                self.error = self.getContinuousError(self.setpoint - input)

                if self.pidInput.getPIDSourceType() == self.PIDSourceType.kRate:
                    if self.P != 0:
                        self.totalError = self.clamp(
                            self.totalError + self.error, 
                            self.minimumOutput / self.P,
                            self.maximumOutput / self.P)
                        
                    self.result = self.P * self.totalError + \
                        self.D * self.error + \
                        self.calculateFeedForward()

                else:

                    if self.I != 0:
                        self.totalError = self.clamp(
                            self.totalError + self.error,
                            self.minimumOutput / self.I,
                            self.maximumOutput / self.I)

                    self.result = self.P * self.error + \
                            self.I * self.totalError + \
                            self.D * (self.error - self.prevError) + \
                            self.calculateFeedForward()
                            
                self.prevError = self.error

                self.result = self.clamp(self.result, self.minimumOutput, self.maximumOutput)

                pidOutput = self.pidOutput
                result = self.result
                
                self.buf.append(self.error)

            pidOutput(result)
            
    def calculateFeedForward(self):
        """Calculate the feed forward term
        
        Both of the provided feed forward calculations are velocity feed forwards.
        If a different feed forward calculation is desired, the user can override
        this function and provide his or her own. This function  does no
        synchronization because the PIDController class only calls it in
        synchronized code, so be careful if calling it oneself.
        
        If a velocity PID controller is being used, the F term should be set to 1
        over the maximum setpoint for the output. If a position PID controller is
        being used, the F term should be set to 1 over the maximum speed for the
        output measured in setpoint units per this controller's update period (see
        the default period in this class's constructor).
        """
        if self.pidInput.getPIDSourceType() == self.PIDSourceType.kRate:
            return self.F * self.getSetpoint()
        else:
            temp = self.F * self.getDeltaSetpoint()
            self.prevSetpoint = self.setpoint
            self.setpointTimer.reset()
            return temp

    def setPID(self, p, i, d, f=0.0):
        """Set the PID Controller gain parameters.
        Set the proportional, integral, and differential coefficients.

        :param p: Proportional coefficient
        :param i: Integral coefficient
        :param d: Differential coefficient
        :param f: Feed forward coefficient (optional, default is 0.0)
        """
        with self.mutex:
            self.P = p
            self.I = i
            self.D = d
            self.F = f

        if self.pEntry is not None:
            self.pEntry.setDouble(p)
        if self.iEntry is not None:
            self.iEntry.setDouble(i)
        if self.dEntry is not None:
            self.dEntry.setDouble(d)
        if self.fEntry is not None:
            self.fEntry.setDouble(f)

    def getP(self):
        """Get the Proportional coefficient.

        :returns: proportional coefficient
        """
        with self.mutex:
            return self.P

    def getI(self):
        """Get the Integral coefficient

        :returns: integral coefficient
        """
        with self.mutex:
            return self.I

    def getD(self):
        """Get the Differential coefficient.

        :returns: differential coefficient
        """
        with self.mutex:
            return self.D

    def getF(self):
        """Get the Feed forward coefficient.

        :returns: feed forward coefficient
        """
        with self.mutex:
            return self.F

    def get(self):
        """Return the current PID result.
        This is always centered on zero and constrained the the max and min
        outs.

        :returns: the latest calculated output
        """
        with self.mutex:
            return self.result

    def setContinuous(self, continuous=True):
        """Set the PID controller to consider the input to be continuous.
        Rather then using the max and min in as constraints, it considers them
        to be the same point and automatically calculates the shortest route
        to the setpoint.

        :param continuous: Set to True turns on continuous, False turns off
            continuous
        """
        with self.mutex:
            self.continuous = continuous

    def setInputRange(self, minimumInput, maximumInput):
        """Sets the maximum and minimum values expected from the input.
        
        :param minimumInput: the minimum percentage expected from the input
        :param maximumInput: the maximum percentage expected from the output
        """
        with self.mutex:
            if minimumInput > maximumInput:
                raise ValueError("Lower bound is greater than upper bound")
            self.minimumInput = minimumInput
            self.maximumInput = maximumInput
            self.setSetpoint(self.setpoint)

    def setOutputRange(self, minimumOutput, maximumOutput):
        """Sets the minimum and maximum values to write.

        :param minimumOutput: the minimum percentage to write to the output
        :param maximumOutput: the maximum percentage to write to the output
        """
        with self.mutex:
            if minimumOutput > maximumOutput:
                raise ValueError("Lower bound is greater than upper bound")
            self.minimumOutput = minimumOutput
            self.maximumOutput = maximumOutput

    def setSetpoint(self, setpoint):
        """Set the setpoint for the PIDController Clears the queue for GetAvgError().

        :param setpoint: the desired setpoint
        """
        with self.mutex:
            if self.maximumInput > self.minimumInput:
                if setpoint > self.maximumInput:
                    newsetpoint = self.maximumInput
                elif setpoint < self.minimumInput:
                    newsetpoint = self.minimumInput
                else:
                    newsetpoint = setpoint
            else:
                newsetpoint = setpoint
            self.setpoint = newsetpoint
            
            self.buf.clear()
            self.totalError = 0

        if self.setpointEntry is not None:
            self.setpointEntry.setDouble(newsetpoint)

    def getSetpoint(self):
        """Returns the current setpoint of the PIDController.

        :returns: the current setpoint
        """
        with self.mutex:
            return self.setpoint
        
    def getDeltaSetpoint(self):
        """Returns the change in setpoint over time of the PIDController
        
        :returns: the change in setpoint over time
        """
        with self.mutex:
            t = self.setpointTimer.get()
            # During testing/simulation it is possible to get a divide by zero because
            # the threads' calls aren't strictly aligned with the master clock.
            if t:
                return (self.setpoint - self.prevSetpoint) / t
            else:
                return 0.0

    def getError(self):
        """Returns the current difference of the input from the setpoint.

        :return: the current error
        """
        with self.mutex:
            #return self.error
            return self.getContinuousError(self.getSetpoint() - self.pidInput.pidGet())
        
    def setPIDSourceType(self, pidSourceType):
        """Sets what type of input the PID controller will use
        
        :param pidSourceType: the type of input
        """
        self.pidInput.setPIDSourceType(pidSourceType)
        
    def getPIDSourceType(self, pidSourceType):
        """Returns the type of input the PID controller is using
        
        :returns: the PID controller input type
        """
        return self.pidInput.getPIDSourceType()
    
    def getAvgError(self):
        """Returns the current difference of the error over the past few iterations.
        You can specify the number of iterations to average with
        :meth:`setToleranceBuffer` (defaults to 1). getAvgError() is used for the
        onTarget() function.
        
        :returns: the current average of the error
        """
        with self.mutex:
            l = len(self.buf)
            if l == 0:
                return 0
            else:
                return sum(self.buf) / l
            
    def isAvgErrorValid(self):
        """Returns whether or not any values have been collected. If no values
        have been collected, getAvgError is 0, which is invalid.
        
        :returns: True if :meth:`getAvgError` is currently valid.
        """
        with self.mutex:
            return len(self.buf) != 0

    def setAbsoluteTolerance(self, absvalue):
        """Set the absolute error which is considered tolerable for use with
        :func:`onTarget`.

        :param absvalue: absolute error which is tolerable in the units of the
            input object
        """
        with self.mutex:
            self.onTarget = lambda: \
                    self.AbsoluteTolerance_onTarget(absvalue)

    def setPercentTolerance(self, percentage):
        """Set the percentage error which is considered tolerable for use with
        :func:`onTarget`. (Input of 15.0 = 15 percent)

        :param percentage: percent error which is tolerable
        """
        with self.mutex:
            self.onTarget = lambda: \
                    self.PercentageTolerance_onTarget(percentage)
                    
    def setToleranceBuffer(self, bufLength):
        """Set the number of previous error samples to average for tolerancing. When
        determining whether a mechanism is on target, the user may want to use a
        rolling average of previous measurements instead of a precise position or
        velocity. This is useful for noisy sensors which return a few erroneous
        measurements when the mechanism is on target. However, the mechanism will
        not register as on target for at least the specified bufLength cycles.
        
        :param bufLength: Number of previous cycles to average.
        :type bufLength: int
        """
        with self.mutex:
            self.buf = deque(list(islice(self.buf, bufLength)), maxlen=bufLength)
        
    def onTarget(self):
        """Return True if the error is within the percentage of the total input
        range, determined by setTolerance. This assumes that the maximum and
        minimum input were set using :func:`setInput`.

        :returns: True if the error is less than the tolerance
        """
        raise ValueError("No tolerance value set when using PIDController.onTarget()")

    def enable(self):
        """Begin running the PIDController."""
        with self.mutex:
            self.enabled = True

        if self.enabledEntry is not None:
            self.enabledEntry.setBoolean(True)

    def disable(self):
        """Stop running the PIDController, this sets the output to zero before
        stopping."""
        with self.mutex:
            self.pidOutput(0)
            self.enabled = False

        if self.enabledEntry is not None:
            self.enabledEntry.setBoolean(False)

    def isEnabled(self):
        """Return True if PIDController is enabled."""
        with self.mutex:
            return self.enabled
            
    def reset(self):
        """Reset the previous error, the integral term, and disable the
        controller."""
        with self.mutex:
            self.disable()
            self.prevError = 0
            self.totalError = 0
            self.result = 0

    def removeListeners(self):
        if self.pEntry is not None:
            self.pEntry.removeListener(self.pListener)
            self.pEntry = None
        if self.iEntry is not None:
            self.iEntry.removeListener(self.iListener)
            self.iEntry = None
        if self.dEntry is not None:
            self.dEntry.removeListener(self.dListener)
            self.dEntry = None
        if self.fEntry is not None:
            self.fEntry.removeListener(self.fListener)
            self.fEntry = None
        if self.setpointEntry is not None:
            self.setpointEntry.removeListener(self.setpointListener)
            self.setpointEntry = None
        if self.enabledEntry is not None:
            self.enabledEntry.removeListener(self.enabledListener)
            self.enabledEntry = None

    def getSmartDashboardType(self):
        return "PIDController"

    def pChanged(self, entry, key, value, param):
        with self.mutex:
            self.P = value

    def iChanged(self, entry, key, value, param):
        with self.mutex:
            self.I = value

    def dChanged(self, entry, key, value, param):
        with self.mutex:
            self.D = value

    def fChanged(self, entry, key, value, param):
        with self.mutex:
            self.F = value

    def setpointChanged(self, entry, key, value, param):
        if self.getSetpoint() != value:
            self.setSetpoint(value)

    def enabledChanged(self, entry, key, value, param):
        if self.isEnabled() != value:
            if value:
                self.enable()
            else:
                self.disable()

    def initTable(self, table):
        self.removeListeners()
        if table is not None:
            self.pEntry = table.getEntry("p")
            self.pEntry.setDouble(self.getP())
            self.iEntry = table.getEntry("i")
            self.iEntry.setDouble(self.getI())
            self.dEntry = table.getEntry("d")
            self.dEntry.setDouble(self.getD())
            self.fEntry = table.getEntry("f")
            self.fEntry.setDouble(self.getF())
            self.setpointEntry = table.getEntry("setpoint")
            self.setpointEntry.setDouble(self.getSetpoint())
            self.enabledEntry = table.getEntry("enabled")
            self.enabledEntry.setBoolean(self.isEnabled())

            self.pListener = self.pEntry.addListener(
                self.pChanged, 
                NetworkTables.NotifyFlags.UPDATE | NetworkTables.NotifyFlags.NEW)
            self.iListener = self.pEntry.addListener(
                self.iChanged, 
                NetworkTables.NotifyFlags.UPDATE | NetworkTables.NotifyFlags.NEW)
            self.dListener = self.pEntry.addListener(
                self.dChanged, 
                NetworkTables.NotifyFlags.UPDATE | NetworkTables.NotifyFlags.NEW)
            self.fListener = self.pEntry.addListener(
                self.fChanged, 
                NetworkTables.NotifyFlags.UPDATE | NetworkTables.NotifyFlags.NEW)
            self.setpointListener = self.pEntry.addListener(
                self.setpointChanged, 
                NetworkTables.NotifyFlags.UPDATE | NetworkTables.NotifyFlags.NEW)
            self.enabledListener = self.pEntry.addListener(
                self.enabledChanged, 
                NetworkTables.NotifyFlags.UPDATE | NetworkTables.NotifyFlags.NEW)
        else:
            self.pEntry = None
            self.iEntry = None
            self.dEntry = None
            self.fEntry = None
            self.setpointEntry = None
            self.enabledEntry = None

    def getContinuousError(self, error):
        """
        Wraps error around for continuous inputs. The original error is
        returned if continuous mode is disabled. This is an unsynchronized
        function.

        :param error: The current error of the PID controller.
        :return: Error for continuous inputs.
        """
        if self.continuous and abs(error) > (self.maximumInput - self.minimumInput) / 2:
            if error > 0:
                return error - (self.maximumInput - self.minimumInput)
            else:
                return error + (self.maximumInput - self.minimumInput)
        return error

    def startLiveWindowMode(self):
        self.disable()

    def stopLiveWindowMode(self):
        pass

    def clamp(self, value, low, high):
        return max(low, min(value, high))
